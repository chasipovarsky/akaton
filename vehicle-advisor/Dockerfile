# ── שלב 1: בנייה של Angular ──────────────────────────────────────────────────
FROM node:22-slim AS build-frontend
WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npx ng build --configuration=production

# ── שלב 2: שרת Python ────────────────────────────────────────────────────────
FROM python:3.12-slim
WORKDIR /app

# תלויות Python
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# קוד הבקאנד
COPY backend/ ./backend/

# תוצר הבנייה של Angular
COPY --from=build-frontend /app/frontend/dist/frontend/browser ./frontend/dist/frontend/browser

WORKDIR /app/backend
ENV PORT=8000
EXPOSE 8000

CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT}
