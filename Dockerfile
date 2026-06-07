# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# 1. Install system dependencies (Cached: changes only if you modify this list)
RUN apt-get update && apt-get install -y \
    gcc \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    libglib2.0-0 \
    libgobject-2.0-0 \
    libharfbuzz0b \
    libfribidi0 \
    && rm -rf /var/lib/apt/lists/*

# 2. Copy ONLY the requirements file first (Cached: changes only if dependencies change)
COPY requirements.txt /app/requirements.txt

# 3. Install Python packages (Cached: skips entirely during normal code edits)
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copy the rest of your application code last (This breaks cache, but it's instant)
COPY . /app

# Make port 10000 available to the world outside this container
EXPOSE 10000

# Define environment variables
ENV PORT=10000
ENV FLASK_APP=app.py

# Run gunicorn when the container launches
CMD ["gunicorn", "-b", "0.0.0.0:10000", "app:app"]
