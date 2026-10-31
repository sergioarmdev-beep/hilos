# Hilos — mini Reddit con FastAPI + Jinja (SSR) + SQLite/Turso

Posts, comentarios y votos ▲▼. Sin login: cada visitante recibe una cookie anónima
(para no votar dos veces) y puede escribir un nombre.

## Local (usa un SQLite en `local.db`, sin configurar nada)
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    uvicorn api.index:app --reload

## Base de datos en producción: Turso
Vercel no guarda archivos, así que en producción se usa Turso (SQLite en la nube).

    turso db create hilos
    turso db show hilos --url          # -> TURSO_DATABASE_URL
    turso db tokens create hilos       # -> TURSO_AUTH_TOKEN

Las tablas se crean solas en la primera petición.

## Desplegar en Vercel
    npm i -g vercel
    vercel                             # primer despliegue
    vercel env add TURSO_DATABASE_URL
    vercel env add TURSO_AUTH_TOKEN
    vercel --prod

Sin esas variables en Vercel, la app usa `/tmp` y los datos se pierden en cada arranque en frío.
