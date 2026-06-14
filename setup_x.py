import asyncio
from twscrape import API

async def main():
    api = API()
    print("⏳ Přidávám účet do lokální databáze twscrape...")
    
    # ZDE VYPLŇ ÚDAJE KE SVÉMU X ÚČTU (nebo fake účtu)
    # Formát: "X_jméno_uživatele", "X_heslo", "Email_k_účtu", "Heslo_k_emailu"
    await api.pool.add_account("AkcieEa", "Akcie123.", "botakcie@seznam.cz", "Akcie123.")
    
    print("⏳ Přihlašuji...")
    await api.pool.login_all()
    print("✅ Hotovo! Účet je bezpečně uložen. Tento soubor už nebudeš potřebovat.")

if __name__ == "__main__":
    asyncio.run(main())