# extract_features.py
import pandas as pd
from urllib.parse import quote_plus

from sqlalchemy import create_engine

conn_str = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    "SERVER=capstone-sqlserver.database.windows.net;"
    "DATABASE=pipelinedb;"
    "Authentication=ActiveDirectoryInteractive;"  # or use SQL auth
)

engine = create_engine(f"mssql+pyodbc:///?odbc_connect={quote_plus(conn_str)}")

with open("feature_extraction.sql") as f:
    sql = f.read()

with engine.connect() as conn:
    df = pd.read_sql(sql, conn)

engine.dispose()

print(f"Rows: {len(df)}, Columns: {len(df.columns)}")
print(df.dtypes)
print(df.describe())

df.to_csv("pipeline_features.csv", index=False)
print("Saved pipeline_features.csv")