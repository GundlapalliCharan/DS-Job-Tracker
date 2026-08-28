import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

response = client.responses.create(
    model="gpt-5.6-luna",
    input="Explain what a Data Analyst does in one sentence."
)

print(response.output_text)