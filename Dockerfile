# Use an official Python runtime as a parent image
FROM python:3.10-slim

# Set the working directory in the container
WORKDIR /service

# Copy the requirements file into the container at /service
COPY requirements.txt .

# Install any needed packages specified in requirements.txt
# We use --default-timeout to avoid issues in some environments and --no-cache-dir to keep the image size down
RUN pip install --default-timeout=100 --no-cache-dir -r requirements.txt

# Copy the entire 'app' directory into the container at /service/app
COPY ./app ./app

# Expose the port the app runs on
# Cloud Run injects the PORT environment variable, Uvicorn will use it by default if --port is not set
# Or we can be explicit. Google Cloud Run expects 8080 by default.
EXPOSE 8080

# Command to run the application using uvicorn
# This assumes your FastAPI app instance is named 'app' in 'app/main.py'
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
