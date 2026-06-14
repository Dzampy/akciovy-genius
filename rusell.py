import pandas as pd
import requests
import io
import urllib3

# Vypnutí varování v terminálu o "nezabezpečeném" spojení (ignorujeme chybějící certifikáty na Macu)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def download_russell_2000():
    print("⏳ Stahuji aktuální seznam tickerů pro Russell 2000...")
    
    # Nový, 100% fungující zdroj
    russell_url = "https://raw.githubusercontent.com/derekbanas/Python4Finance/main/Russell2000.csv"
    
    try:
        # verify=False = přikáže Pythonu, ať ignoruje chybějící certifikáty na Macu
        response = requests.get(russell_url, verify=False)
        response.raise_for_status()

        # Zpracování textu do tabulky Pandas
        df = pd.read_csv(io.StringIO(response.text), header=None)
        
        # Předpokládáme, že tickery jsou v prvním sloupci
        tickers = df[0].tolist()

        # Očištění tickerů (odstranění mezer, zbytečností na začátku/konci a převod na velká písmena)
        clean_tickers = [str(t).strip().upper() for t in tickers if str(t).strip() and str(t).strip() != "Symbol"]
        
        # Speciální ošetření pro Yahoo Finance (. na -)
        clean_tickers = [t.replace('.', '-') for t in clean_tickers]

        # Zápis do textového souboru
        filename = "russell2000.txt"
        with open(filename, "w") as f:
            for ticker in clean_tickers:
                f.write(f"{ticker}\n")

        print(f"✅ Úspěšně uloženo {len(clean_tickers)} tickerů do souboru '{filename}'.")

    except requests.exceptions.HTTPError as err:
        print(f"❌ Chyba 404 nebo jiná HTTP chyba: {err}")
    except Exception as e:
        print(f"❌ Nastala jiná chyba při stahování: {e}")

if __name__ == "__main__":
    download_russell_2000()