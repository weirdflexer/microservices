FROM docker.io/library/python:3.13-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common ./common
COPY services/__init__.py ./services/__init__.py
COPY services/bookings ./services/bookings
RUN useradd --create-home appuser && chown -R appuser:appuser /app
USER appuser
EXPOSE 8000
CMD ["uvicorn", "services.bookings.main:app", "--host", "0.0.0.0", "--port", "8000"]
