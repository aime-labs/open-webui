## Setup

Clone the repo:

```bash
    cd /your/desired/destination
    git clone -b main_aime https://github.com/aime-labs/open-webui/
```

Create virtual environment and activate it:

```bash
    cd /your/desired/destination/open-webui/backend
    python3 -m venv venv
    source /your/desired/destination/open-webui/backend/venv/bin/activate
```

Install requirements in virtual environment:

pip install -r requirements.txt

# Building Frontend Using Node
If not installed, install NPM with:

```bash
sudo apt install npm
```
Navigate to the 

```bash
    cd /your/desired/destination/open-webui
    npm install
    npm run build
```

# Copying required .env file

```bash
    cp -RPp /your/desired/destination/open-webui/.env.example /your/desired/destination/open-webui/.env
```

## Run open-webui backend
If not active, activate the virtual environment:

```bash
    source /your/desired/destination/open-webui/backend/venv/bin/activate
```

To use non-default host (http://localhost) and port (8080), run:
```bash
    export HOST = <your_desired_host>
    export PORT = <your_desired_port>   
```
or make entries in /your/desired/destination/open-webui/.env


Start the backend with:
```bash
    bash start.sh
```





