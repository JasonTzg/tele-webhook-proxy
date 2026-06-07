# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies required by WeasyPrint
# RUN apt-get update && apt-get install -y \
#     build-essential \
#     python3-dev \
#     python3-pip \
#     python3-setuptools \
#     python3-wheel \
#     python3-cffi \
#     libcairo2 \
#     libpango-1.0-0 \
#     libpangocairo-1.0-0 \
#     libgdk-pixbuf-2.0-0 \
#     libffi-dev \
#     shared-mime-info \
#     && rm -rf /var/lib/apt/lists/*
RUN apt-get update && apt-get install -y \
    gcc \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

# Copy the current directory contents into the container at /app
COPY . /app

# Install any needed packages specified in requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Make port 10000 available to the world outside this container
EXPOSE 10000

# Define environment variable
ENV PORT=10000
ENV FLASK_APP=app.py

# Run gunicorn when the container launches
CMD ["gunicorn", "-b", "0.0.0.0:10000", "app:app"]
