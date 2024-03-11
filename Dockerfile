FROM python:3.11-slim as builder 

# Use a less-privileged user for the runtime image
RUN adduser --shell /bin/bash --home /app appuser
USER appuser

# Working directory: Create an app directory
WORKDIR /app

ENV FLASK_APP=app/app.py
ENV PATH=$PATH:/app/.local/bin
ENV TZ="Etc/GMT+3"

# Copy project requirements
COPY pyproject.toml poetry.lock ./

# Install Poetry 
RUN pip install poetry

# Install dependencies 
RUN poetry install --no-dev 

# Copy the rest of application code
COPY . .

# Expose application's port
EXPOSE 5000

# Command to run the application
CMD ["poetry", "run", "flask", "run", "--host", "0.0.0.0"]
