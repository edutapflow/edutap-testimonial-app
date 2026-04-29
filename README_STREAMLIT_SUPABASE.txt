EduTap Streamlit + Supabase update

FILES TO COPY INTO PROJECT ROOT:
- app.py
- main.py
- google_clients.py
- supabase_store.py
- requirements.txt
- packages.txt
- .streamlit/config.toml
- .streamlit/secrets.toml.example
- .gitignore

KEEP THESE FOLDERS IN PROJECT ROOT:
- assets/
- output/ (can be auto-created)

LOCAL TEST:
1. Create .streamlit/secrets.toml from .streamlit/secrets.toml.example.
2. Add real OPENAI and SUPABASE secrets.
3. Run:
   streamlit run app.py

GITHUB:
Do not upload .env, .streamlit/secrets.toml, credentials/, output/, data/.

STREAMLIT CLOUD:
Main file path: app.py
Add secrets in Streamlit Cloud Settings > Secrets using the same TOML keys.
