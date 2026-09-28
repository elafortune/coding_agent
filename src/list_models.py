"""Liste les modèles Gemini disponibles pour la clé API configurée."""

import os

from dotenv import load_dotenv
from google import genai

load_dotenv()

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
for m in client.models.list():
    actions = m.supported_actions or []
    if "generateContent" in actions:
        print(m.name)
