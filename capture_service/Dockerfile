# Imagem oficial do Playwright para Python, ja com os navegadores instalados
# (evita o passo de "playwright install" na hora do build, que baixa varios
# MB de binario toda vez). Confira se a tag ainda existe e, se nao, troque
# pela versao atual publicada em mcr.microsoft.com/playwright/python.
FROM mcr.microsoft.com/playwright/python:v1.47.0-jammy

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
