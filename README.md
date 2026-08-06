# Instagram Bot - Follow & Unfollow

Tool desktop (Tkinter + Selenium) che unifica in un'unica GUI a tab:

- **🎯 Auto Follow** — follow automatico da coda persistente o via ricerca hashtag ("Deep Search"), con rilevamento rate-limit, batch cooldown e retry.
- **🚫 Unfollow** — smette di seguire automaticamente chi non ti segue indietro, calcolato dai file `followers.json` / `following.json` esportati da Instagram. Riusa lo stesso browser/login del tab Follow.
- **📋 Follow Queue** — gestione della coda utenti da seguire (import/export, aggiunta/rimozione manuale).
- **⚙️ Settings** — parametri di estrazione, timing di follow/unfollow e impostazioni tecniche, persistiti in `bot_config.json`.
- **📝 Logs** — log colorato in tempo reale, esportabile su file.

## ⚠️ Avviso importante

Questo strumento automatizza azioni su Instagram (scraping di profili/hashtag, follow/unfollow massivo) tramite un browser controllato da Selenium, incluse misure per ridurre il rilevamento come bot. Questo genere di automazione **viola i Termini di Servizio di Instagram** e può portare a limitazioni temporanee o al ban dell'account usato. Usalo a tuo rischio, con un account che sei disposto a perdere, e mantieni delay/limiti conservativi.

## Setup

```bash
pip install -r requirements.txt
python instagram_bot.py
```

Richiede Google Chrome installato: `webdriver-manager` scarica automaticamente il ChromeDriver compatibile.

## Uso rapido

### Follow
1. Tab **Auto Follow** → **Open Browser**, esegui il login manuale su Instagram.
2. Scegli la modalità: **Follow from Queue** (segue utenti già in coda) oppure **Deep Search** (cerca nuovi utenti tramite hashtag e li aggiunge in coda).
3. Imposta delay min/max e limite follow per la sessione, poi **Start Following**.

### Unfollow
1. Da Instagram: **Impostazioni → Privacy e sicurezza → Scarica i tuoi dati**, richiedi l'export in formato JSON e scarica `followers_1.json` (o simile) e `following.json`.
2. Tab **Auto Follow** → **Open Browser** (se non già aperto) e fai login.
3. Tab **🚫 Unfollow** → **Carica JSON**, seleziona i due file. Il tool calcola automaticamente chi segui ma non ti segue indietro.
4. Imposta delay min/max e limite sessione, poi **Start Unfollow**. Il progresso viene salvato in `unfollow_progress.json`, quindi puoi fermarti e riprendere in sessioni successive senza ripartire da zero.

## File generati (esclusi da git)

`chrome_profile/`, `follow_queue.json`, `followed_history.json`, `user_frequencies.json`, `hashtags.json`, `bot_config.json`, `unfollow_progress.json`, `unfollow_last_session.json`, log vari — vedi `.gitignore`.

## Licenza

GPL-3.0, vedi [LICENSE](LICENSE).
