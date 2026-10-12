# Image unique de MédiRDV : l'API et l'interface partagent le même code ; la commande les distingue (docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dépendances d'abord : cette couche n'est reconstruite que si requirements.txt change.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY backend ./backend
COPY frontend ./frontend
COPY data ./data
COPY .streamlit ./.streamlit

# Utilisateur sans privilèges ; /sortie reçoit le fichier de clés, /app/.secret_jwt le secret des jetons.
RUN useradd --create-home --uid 10001 medirdv \
    && mkdir -p /sortie /sauvegardes \
    && chown -R medirdv:medirdv /app /sortie /sauvegardes
USER medirdv

EXPOSE 8000 8501
CMD ["python", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
