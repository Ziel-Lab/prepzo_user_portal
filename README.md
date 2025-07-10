# Prepzo User Portal

## Overview

Prepzo User Portal is a production-ready web application designed to provide a seamless user experience for mock interviews, career tools, and document management. The project leverages Flask for the backend, integrates with LiveKit for real-time interview sessions, and supports both local and Docker-based deployments.

## Features

- Mock interview sessions with AI agents (LiveKit integration)
- Career tools (cover letter, resume analysis, LinkedIn optimization)
- Document management and storage
- Production-ready deployment with Gunicorn/Waitress
- Docker support for easy setup
- Modular codebase for extensibility

## Table of Contents
- [Overview](#overview)
- [Features](#features)
- [Project Structure](#project-structure)
- [Setup & Installation](#setup--installation)
  - [Docker (Recommended)](#docker-recommended)
  - [Manual Setup](#manual-setup)
- [Usage](#usage)
- [Key Files](#key-files)
  - [Dockerfile](#dockerfile)
  - [run_all.py](#run_allpy)
  - [run.py](#runpy)
- [Contributing](#contributing)
- [License](#license)

## Project Structure

```
prepzo_user_portal/
├── app/
│   ├── auth/
│   ├── main/
│   ├── userPortal/
│   ├── utils/
│   ├── extensions.py
│   ├── secrets.py
│   └── __init__.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── run.py
├── run_all.py
├── start_agent.py
└── ...
```

## Setup & Installation

### Docker (Recommended)

1. **Build the Docker image:**
   ```sh
   docker build -t prepzo-user-portal .
   ```
2. **Run the container:**
   ```sh
   docker run -p 5000:5000 prepzo-user-portal
   ```
   The app will be available at [http://localhost:5000](http://localhost:5000).

### Manual Setup

1. **Clone the repository:**
   ```sh
   git clone <repo-url>
   cd prepzo_user_portal
   ```
2. **Install dependencies:**
   ```sh
   pip install -r requirements.txt
   ```
3. **Set up environment variables:**
   - Create a `.env` file in the root directory and add necessary environment variables (see `.env.example` if available).
4. **Run the application:**
   - For development:
     ```sh
     python run.py
     ```
   - For production (recommended):
     ```sh
     python run_all.py
     ```
     This will start both the Flask web app and the LiveKit agent.

## Usage

- Access the web app at [http://localhost:5000](http://localhost:5000).
- Use the mock interview and career tools as per the UI.
- For production, use `run_all.py` to ensure both the web server and agent are running.

## Key Files

### Dockerfile
- Defines the production Docker image for the app.
- Installs dependencies, copies code, and sets up Gunicorn as the WSGI server.
- Exposes port 5000 for the Flask app.

### run_all.py
- Orchestrates both the Flask web server and the LiveKit agent.
- Handles process management, signal handling, and logs output from both services.
- Recommended entry point for production deployments.

### run.py
- Entry point for running the Flask app (development mode).
- Loads environment variables and creates the Flask app instance.


---