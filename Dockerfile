# Použijeme lehkou a stabilní verzi Pythonu
FROM python:3.11-slim

# Nastavení pracovního adresáře uvnitř kontejneru
WORKDIR /app

# Instalace závislostí: build-essential a CHROMIUM pro Kaleido/Plotly grafy
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    chromium \
    && rm -rf /var/lib/apt/lists/*

# Kopírování requirements a instalace knihoven
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopírování celého zbytku kódu do kontejneru
COPY . .

# Příkaz, který spustí bota při startu
CMD ["python", "akciovygenius.py"]