# -*- coding: utf-8 -*-
"""business-data-analysis-chatbot-agent-langchain.ipynb

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/1Z81NndHkYZyWAZTj0D2gYumN4qvShpzq

# Business and Data Analysis Chatbot
### LangChain, HuggingFace, SQL, RAG

LLMs are great for many general tasks.

But they're even more powerful when integrated with rich SQL data and time-series prediction capabilities.

I wanted to create a conversational assistant that can help analyze a database of real-estate/home prices, and generate forecasts based on historical data as well.

It should be able to answer questions like:

* What was the average home rental in zip code 01012 for the past year?
* How much has the seasonally adjusted home rent increased in Denver, CO from 2020 to 2023?
* Based on home rents in Hampshire County, MA for the last 4 years, create a forecast for the next 12 months

I'll be using:

1. OpenAI's GPT-3.5 as the LLM
2. LangChain for the chatbot
3. Retrieval Augmented Generation (RAG) from an SQL database
4. HuggingFace dataset for Zillow home rents
5. Amazon's Chronos T5 model for forecasts/predictions

Let's install the required packages:
"""

!pip install datasets langchain langchain-community langchain-openai
!pip install git+https://github.com/amazon-science/chronos-forecasting.git

"""Then set up the Chronos model:

(Note: you'll need a good GPU to run this model)
"""

import torch
from chronos import ChronosPipeline

pipeline = ChronosPipeline.from_pretrained(
    "amazon/chronos-t5-base",
    device_map="cuda",
    torch_dtype=torch.bfloat16,
)

"""A quick function that takes historical data and returns future predictions, alongside outputting a graph:"""

import matplotlib.pyplot as plt
import numpy as np


def chronos_prediction(historical, length):
    context = torch.tensor(historical)
    forecast = pipeline.predict(context, length)

    forecast_index = range(len(historical), len(historical) + length)
    low, median, high = np.quantile(forecast[0].numpy(), [0.1, 0.5, 0.9], axis=0)

    plt.figure(figsize=(8, 4))
    plt.plot(historical, color="blue", label="Historical")
    plt.plot(forecast_index, median, color="green", label="Median Forecast")
    plt.fill_between(
        forecast_index,
        low,
        high,
        color="green",
        alpha=0.3,
        label="80% prediction interval",
    )
    plt.legend()
    plt.grid()
    plt.show()

    return median


chronos_prediction(
    [
        347,
        305,
        336,
        340,
        318,
        362,
        348,
        363,
        435,
        491,
        505,
        404,
        359,
        310,
        337,
        360,
        342,
        406,
        396,
        420,
        472,
        548,
        559,
        463,
        407,
        362,
        405,
        417,
        391,
        419,
    ],
    12,
)

"""Our Zillow database from HuggingFace:"""

from datasets import load_dataset

dataset = load_dataset("misikoff/zillow-viewer", "rentals")["train"]

dataset[1]

"""The Zillow HF dataset has different rows for various zip codes, cities, counties, and MSAs.

I'm only interested in the zip code data. So for a given zip code, I want to get the city, county, and state the zip code is in based on this CSV from GitHub:
"""

import pandas

zipData = pandas.read_csv(
    "https://raw.githubusercontent.com/scpike/us-state-county-zip/master/geo-data.csv"
)
zipHash = {}

for row in zipData.iterrows():
    zipHash[row[1]["zipcode"]] = {
        "city": row[1]["city"],
        "county": row[1]["county"],
        "state": row[1]["state_abbr"],
    }

zipHash["01012"]

zipHash["01013"]

"""Connect to an SQLite database and create the table:"""

from langchain_community.utilities import SQLDatabase

db = SQLDatabase.from_uri("sqlite:///zillow.db")

db.run(
    """CREATE TABLE rentals (
id INTEGER PRIMARY KEY AUTOINCREMENT,
zip VARCHAR,
city VARCHAR,
county VARCHAR,
state VARCHAR,
home_type INTEGER,
date DATETIME,
rent FLOAT,
rent_adjusted FLOAT
)"""
)

"""The dataset has many missing/null columns.

We need to filter the dataset where `Region Type` is `0` (those are the zip code rows) and `Rent (Smoothed)` is not empty.

Some zip codes also don't have the leading 0, we need to add that.

The rentals dataset has 1M+ rows, but I'm limiting them to 10K here.

(This can take a while. Using `INSERT INTO` with multiple sets of values would probably be more efficient...)
"""

row_count = 0
for i in range(len(dataset)):
    row = dataset[i]

    if row["Region Type"] != 0 or row["Rent (Smoothed)"] is None:
        continue

    zip = f"0{row['Region']}" if len(row["Region"]) < 5 else row["Region"]
    geo = zipHash.get(zip)

    if geo is None:
        continue

    db.run(
        "INSERT INTO rentals (zip, city, county, state, home_type, date, rent, rent_adjusted) VALUES (:zip, :city, :county, :state, :home_type, :date, :rent, :rent_adjusted)",
        parameters={
            "zip": zip,
            "city": geo["city"],
            "county": geo["county"],
            "state": geo["state"],
            "home_type": row["Home Type"],
            "date": row["Date"].isoformat(),
            "rent": row["Rent (Smoothed)"],
            "rent_adjusted": row["Rent (Smoothed) (Seasonally Adjusted)"],
        },
    )

    row_count += 1
    if row_count >= 10000:
        break

"""Reconnect to the database so we have the updated table schema and details:"""

from pprint import pprint

db = SQLDatabase.from_uri("sqlite:///zillow.db")

pprint(db.run("select * from rentals limit 5"))
# pprint(db.run('select count() as count, city, state from rentals group by city order by count desc limit 10'))

"""OpenAI's API key:"""

import getpass

api_key = getpass.getpass()

"""Using a simple in-memory hash to store the chat history/sessions (easier for demonstration).

Also a function to send only the latest 10 messages to the LLM so we don't overflow its context:
"""

from langchain_community.agent_toolkits import SQLDatabaseToolkit
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import (
    SystemMessage,
    HumanMessage,
    AIMessage,
    ToolMessage,
    ToolMessageChunk,
)
from langchain_community.chat_message_histories import ChatMessageHistory
from langchain_core.chat_history import BaseChatMessageHistory
from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_core.tools import tool

store = {}


def get_session_history(id):
    if id not in store:
        store[id] = ChatMessageHistory()
    return store[id]


def limit(input):
    return {**input, "messages": input["messages"][-10:]}

"""Creating a few tools/functions for the LLM so it can:

1. See the table names and schemas in the database
2. Run SQL queries
3. Get current date and time (if needed to answer the user's question)
4. Generate time-series predictions using the Chronos model

Also creating a system prompt and template.

(The tool descriptions provided by `SQLDatabaseToolkit` could probably be better, but I'm leaving them as in for now)
"""

from datetime import datetime
from typing import List
from langchain_openai import ChatOpenAI
import json

model = ChatOpenAI(model="gpt-3.5-turbo-0125", openai_api_key=api_key)

toolkit = SQLDatabaseToolkit(db=db, llm=model)
sql_tools = toolkit.get_tools()

sql_tools[0].name = "query_sql_database_tool"
sql_tools[1].name = "info_sql_database_tool"
sql_tools[2].name = "list_sql_database_tool"
sql_tools[3].name = "query_sql_checker_tool"


@tool
def get_current_datetime(current: bool):
    """Get current date and time in ISO format"""
    return datetime.now().isoformat()


@tool
def get_time_series_prediction(
    historical_values: List[float], number_of_values_to_predict: int
):
    """Use this tool to generate possible future predictions based on past time series data.
    Provide a list of numbers as 'historical_values', and specify how many future values to predict in 'number_of_values_to_predict'
    This tool returns the predicted list of numbers representing median trends/forecasts. It'll also output a visual graph.
    """
    pred = chronos_prediction(historical_values, number_of_values_to_predict)
    return json.dumps(pred.tolist())


toolsHash = {
    "query_sql_database_tool": sql_tools[0],
    "info_sql_database_tool": sql_tools[1],
    "list_sql_database_tool": sql_tools[2],
    "query_sql_checker_tool": sql_tools[3],
    "get_current_datetime": get_current_datetime,
    "get_time_series_prediction": get_time_series_prediction,
}

model = model.bind_tools([*sql_tools, get_current_datetime, get_time_series_prediction])

system_prompt = """You are an assistant designed to help with business and data analysis.
If the user asks for data you don't have, use the provided tools/functions to interact with a database; follow these steps:

1. First, you should ALWAYS look at the tables in the database to see what you can query. Do NOT skip this step

2. Then query the schema of the most relevant tables

3. Create a syntactically correct SQLite query

4. You MUST use the tool to check/validate your query syntax before executing it. If you get an error while executing a query, rewrite the query and try again

5. Run the query, look at the results, and only use this returned information to construct your final answer

Guidelines:

Do not use the 'multi_tool_use.parallel' tool, call each tool individually.
Unless the user specifies a specific number of examples they wish to obtain, always limit your query to at most 5 results.
You can order the results by a relevant column to return the most interesting examples in the database.
Never query for all the columns from a specific table, only ask for the relevant columns given the question.
DO NOT make any DML statements (INSERT, UPDATE, DELETE, DROP etc.) to the database."""

template = ChatPromptTemplate.from_messages(
    [SystemMessage(system_prompt), MessagesPlaceholder(variable_name="messages")]
)

"""Putting together the main chain and chat history.

We also need to be able to execute the tools called by the LLM.

You could use LangGraph's built-in agent for this, but I wanted to implement my own.

I've also configured streaming support:
"""

from datetime import datetime
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import chain, RunnableLambda

chain = limit | template | model
with_history = RunnableWithMessageHistory(
    chain, get_session_history, input_messages_key="messages"
)

config = {"configurable": {"session_id": "cat"}}


def agent(input):
    messages = input["messages"]

    while True:
        response = None

        for chunk in with_history.stream(
            {**input, "messages": messages}, config=config
        ):
            yield chunk
            if response is None:
                response = chunk
            else:
                response += chunk

        if response.tool_calls:
            messages = []
            for call in response.tool_calls:
                tool = toolsHash.get(call["name"])
                if tool:
                    output = tool.invoke(call["args"])
                    tool_message = ToolMessageChunk(output, tool_call_id=call["id"])
                    yield tool_message
                    messages.append(tool_message)
        else:
            break

"""Finally, time to run the whole thing!"""

def run(message):
    res = None
    stream = RunnableLambda(agent).stream(
        {"messages": [HumanMessage(message)], "language": "English"}, config=config
    )

    for chunk in stream:
        if res is None:
            res = chunk
        else:
            res += chunk

        if chunk.content:
            print(chunk.content, end="")

    return res


# run('What has been the average home rent in Worcester County for the past year?')
# run('Calculate the average home rent for zip code 07302 for years 2020 and 2023. And by what percentage has the rent changed during this time?')
run(
    "Based on home rents in Newark, NJ for the last 4 years, create a prediction graph for the next 12 months"
)

import streamlit as st
import matplotlib.pyplot as plt
import pandas as pd
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda

# Assuming agent and config are defined elsewhere in your code
# from your_code import agent, config

def run(message):
    res = None
    stream = RunnableLambda(agent).stream(
        {"messages": [HumanMessage(message)], "language": "English"}, config=config
    )

    result = []
    for chunk in stream:
        if res is None:
            res = chunk
        else:
            res += chunk

        if chunk.content:
            result.append(chunk.content)

    return ''.join(result)

# Streamlit app layout
st.title("Business Analysis and Prediction Agent")
st.write("Submit your query to get analysis and predictions.")

# Input field for user message
user_input = st.text_input("Enter your query:", "")

# Add a button to submit the query
if st.button("Submit Query"):
    if user_input:
        # Run the agent with the user's input
        response = run(user_input)

        st.write("### Agent's Response:")
        st.write(response)

        # For demonstration, if the response includes a command to plot a graph
        if "create a prediction graph" in user_input.lower():
            # Example data for plotting
            historical_data = [1500, 1600, 1650, 1700, 1750, 1800, 1850, 1900, 1950, 2000]
            forecast = [2100, 2150, 2200, 2250, 2300, 2350, 2400, 2450, 2500, 2550, 2600, 2650]

            plt.figure(figsize=(10, 5))
            plt.plot(historical_data, label='Historical Data', marker='o')
            forecast_index = range(len(historical_data), len(historical_data) + len(forecast))
            plt.plot(forecast_index, forecast, label='Forecast', marker='o', color='green')
            plt.fill_between(forecast_index, [f * 0.9 for f in forecast], [f * 1.1 for f in forecast], color='green', alpha=0.3)
            plt.xlabel('Month')
            plt.ylabel('Rent')
            plt.title('Home Rent Prediction')
            plt.legend()
            plt.grid()
            st.pyplot(plt)

