# Capstone Project

Backfills Azure DevOps pipeline data, trains an XGBoost model, and serves predictions via a FastAPI backend.

## Project Structure

```text
backfill/        # Data ingestion from Azure DevOps
training/        # Feature extraction, model training, and registration
backend/         # FastAPI inference API
frontend/        # Angular 18 SPA
```

## Live Deployment

| Component | URL |
|-----------|-----|
| Frontend  | https://white-sky-0d2e9850f.7.azurestaticapps.net |
| Backend   | https://capstone-backend-api.azurewebsites.net |
| API Docs  | https://capstone-backend-api.azurewebsites.net/docs |

## Getting Started

### Prerequisites

* Python 3.10+
* Azure CLI (`az`) installed and logged in
* Access granted to Key Vault `capstone-kv-9842`

### 1. Clone and authenticate

```powershell
git clone https://github.com/tanmay-udupa/capstone-project.git
cd capstone-project
az login
```

### 2. Fetch secrets from Key Vault

```powershell
az keyvault secret show --vault-name capstone-kv-9842 --name SQL-PASSWORD --query value -o tsv
az keyvault secret show --vault-name capstone-kv-9842 --name ADO-PAT --query value -o tsv
az keyvault secret show --vault-name capstone-kv-9842 --name BLOB-CONNECTION-STRING --query value -o tsv
az keyvault secret show --vault-name capstone-kv-9842 --name AZURE-OPENAI-API-KEY --query value -o tsv
```

### 3. Set up backend `.env`

```powershell
cd backend
Copy-Item .env.example .env
# Fill in secret values from step 2
```

### 4. Set up backfill environment variables

```powershell
$env:ADO_PAT = az keyvault secret show --vault-name capstone-kv-9842 --name ADO-PAT --query value -o tsv
$env:SQL_PASSWORD = az keyvault secret show --vault-name capstone-kv-9842 --name SQL-PASSWORD --query value -o tsv
$env:BLOB_CONNECTION_STRING = az keyvault secret show --vault-name capstone-kv-9842 --name BLOB-CONNECTION-STRING --query value -o tsv
```

> [!IMPORTANT]
> Never share secrets directly. Credentials are managed through Azure Key Vault.

## Running Components

### Backfill (Data Ingestion)

```powershell
cd backfill
pip install pymssql requests azure-storage-blob

python backfill.py [PROJECT] [MAX_RUNS]
```

### Training

```powershell
cd training
pip install -r requirements.txt

python extract_features.py  # 1. Extract features from SQL
python prepare_data.py      # 2. Prepare train and test splits
python validate_features.py # 3. Validate features
python tune.py              # 4. Tune hyperparameters and train
python register_model.py    # 5. Register model in Azure ML
```

Configuration is in `training/config.json` (subscription, resource group, workspace).

### Backend (API)

```powershell
cd backend
pip install -r requirements.txt

# Place the trained model artifact
Copy-Item ..\training\xgb_best_model.ubj models\

# Run the server
python -m uvicorn main:app --host 0.0.0.0 --port 8000

# View API Documentation
http://localhost:8000/docs#
```

### Frontend

```powershell
cd frontend
npm install
npm start          # dev server at http://localhost:4200
```