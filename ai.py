from openai import OpenAI
import os

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def ai_answer(text):

    try:

        r = client.chat.completions.create(

            model="gpt-4o-mini",

            messages=[
                {"role":"system","content":"Ты справочный бот клиники"},
                {"role":"user","content":text}
            ]
        )

        return r.choices[0].message.content

    except:

        return ""
