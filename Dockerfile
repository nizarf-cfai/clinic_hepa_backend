# Use official lightweight Python image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose the port
EXPOSE 8080

# Command to run the application using Uvicorn
# Cloud Run expects the app to listen on the PORT environment variable (default 8080)
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]