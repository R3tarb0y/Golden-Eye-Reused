from waitress import serve
from app import server

serve(
    server,
    host="127.0.0.1",
    port=8050,
    threads=12,
    connection_limit=200,
    channel_timeout=120,
)       