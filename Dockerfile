FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    python -c "import discord; print('discord ok')" && \
    python -c "import aiohttp; print('aiohttp ok')" && \
    python -c "from playwright.async_api import async_playwright; print('playwright ok')" && \
    python -c "from google import genai; print('genai ok')"

COPY . .

# Print all env vars present at startup (not values, just names)
CMD ["python", "-u", "bot.py"]
