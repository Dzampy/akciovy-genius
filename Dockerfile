# Použijeme lehkou verzi Pythonu 3.11
FROM python:3.11-slim

# Nastavení pracovního adresáře
WORKDIR /app

# Instalace závislostí pro systém (potřebné pro některé knihovny)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Kopírování souboru s knihovnami a jejich instalace
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Kopírování celého kódu bota
COPY . .

# Příkaz, který se spustí při startu kontejneru
CMD ["python", "akciovygenius.py"]