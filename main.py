import os
import json
import uuid
import ssl
from typing import Dict, Any, Optional

import asyncpg
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from pydantic_settings import BaseSettings
from fastapi.middleware.cors import CORSMiddleware

from algorithm import Akinator

# --- Configuration ---
class Settings(BaseSettings):
    database_url: str = os.environ.get(
        "DATABASE_URL",
        "postgresql://user:password@localhost:5432/whodatdev_db"
    )
    dataset_path: str = "data/characters_data.json"
    questions_path: str = "data/questions.json"

    class Config:
        env_file = ".env"
        env_file_encoding = 'utf-8'

settings = Settings()
app = FastAPI()

origins = [
    "https://whodatdev-tau.vercel.app",
    "http://localhost:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Database Connection Pool ---
DB_POOL: Optional[asyncpg.Pool] = None

async def get_db_pool() -> asyncpg.Pool:
    global DB_POOL
    if DB_POOL is None:
        try:
            ssl_context = None
            # Enable SSL for non-local DB
            if not any(host in settings.database_url for host in ["localhost", "127.0.0.1"]):
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE  # Render allows insecure verify

            DB_POOL = await asyncpg.create_pool(
                settings.database_url,
                ssl=ssl_context
            )

            # Ensure tables exist
            async with DB_POOL.acquire() as connection:
                await connection.execute("""
                    CREATE TABLE IF NOT EXISTS game_sessions (
                        session_id UUID PRIMARY KEY,
                        akinator_state JSONB NOT NULL,
                        last_accessed TIMESTAMPTZ DEFAULT NOW() NOT NULL
                    );
                """)
                await connection.execute("""
                    CREATE INDEX IF NOT EXISTS idx_last_accessed
                    ON game_sessions (last_accessed);
                """)

        except Exception as e:
            print(f"🔴 Database connection or table creation failed: {e}")
            raise HTTPException(status_code=503, detail="Database service unavailable or setup failed.")

    return DB_POOL

@app.on_event("startup")
async def startup_event():
    await get_db_pool()
    print("✅ FastAPI application startup complete. Database pool initialized.")

@app.on_event("shutdown")
async def shutdown_event():
    if DB_POOL:
        await DB_POOL.close()
        print("ℹ️ Database pool closed.")

# --- Pydantic Models ---
class AnswerPayload(BaseModel):
    session_id: uuid.UUID
    attribute_key: str
    answer: str

class GuessConfirmationPayload(BaseModel):
    session_id: uuid.UUID
    guessed_character_name: str
    user_confirms_correct: bool

# --- Helper Functions ---
async def get_akinator_instance(session_id: uuid.UUID, pool: asyncpg.Pool) -> Akinator:
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            "SELECT akinator_state FROM game_sessions WHERE session_id = $1", session_id
        )
        if not row:
            raise HTTPException(status_code=404, detail=f"Session ID '{session_id}' not found.")

        try:
            state_dict = json.loads(row['akinator_state'])
            akinator_instance = Akinator(
                dataset_path=settings.dataset_path,
                questions_path=settings.questions_path
            )
            akinator_instance._load_state(state_dict)
            return akinator_instance
        except Exception as e:
            print(f"🔴 Error deserializing Akinator state for session {session_id}: {e}")
            raise HTTPException(status_code=500, detail="Failed to load game state.")

async def save_akinator_state(session_id: uuid.UUID, akinator_instance: Akinator, pool: asyncpg.Pool):
    try:
        state_dict = akinator_instance.get_state()
        state_json = json.dumps(state_dict)
        async with pool.acquire() as connection:
            await connection.execute("""
                UPDATE game_sessions
                SET akinator_state = $1, last_accessed = NOW()
                WHERE session_id = $2
            """, state_json, session_id)
    except Exception as e:
        print(f"🔴 Error serializing Akinator state for session {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to save game state.")

# --- API Endpoints ---
@app.get("/")
async def root():
    return JSONResponse(content={"message": "Welcome to the Who Dat Dev? Akinator API!"})

@app.post("/start_game")
async def start_game_session():
    session_id = uuid.uuid4()
    pool = await get_db_pool()

    try:
        akinator_instance = Akinator(
            dataset_path=settings.dataset_path,
            questions_path=settings.questions_path
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="Dataset not found.")
    except ValueError as e:
        raise HTTPException(status_code=500, detail=str(e))

    initial_game_response = akinator_instance.start_game()
    try:
        state_dict = akinator_instance.get_state()
        state_json = json.dumps(state_dict)
        async with pool.acquire() as connection:
            await connection.execute("""
                INSERT INTO game_sessions (session_id, akinator_state, last_accessed)
                VALUES ($1, $2, NOW())
            """, session_id, state_json)
    except Exception as e:
        print(f"🔴 Error inserting new game session {session_id} into DB: {e}")
        raise HTTPException(status_code=500, detail="Failed to save initial game state.")

    return JSONResponse(content={"session_id": str(session_id), **initial_game_response})

@app.post("/questions")
async def submit_answer(payload: AnswerPayload):
    pool = await get_db_pool()
    akinator_instance = await get_akinator_instance(payload.session_id, pool)

    valid_answers = {"no": 0.0, "probably no": 0.25, "probably yes": 0.75, "yes": 1.0}
    if payload.answer.lower() not in valid_answers:
        raise HTTPException(status_code=400, detail=f"Invalid answer. Expected {list(valid_answers.keys())}")

    game_state_response = akinator_instance.process_answer(payload.attribute_key, valid_answers[payload.answer.lower()])
    await save_akinator_state(payload.session_id, akinator_instance, pool)
    return JSONResponse(content={"session_id": str(payload.session_id), **game_state_response})

@app.post("/confirm_guess")
async def confirm_akinator_guess(payload: GuessConfirmationPayload):
    pool = await get_db_pool()
    akinator_instance = await get_akinator_instance(payload.session_id, pool)

    if payload.user_confirms_correct:
        async with pool.acquire() as connection:
            await connection.execute("DELETE FROM game_sessions WHERE session_id = $1", payload.session_id)
        return JSONResponse(content={
            "session_id": str(payload.session_id),
            "status": "finished_won",
            "message": f"🎉 Great! I knew it was {payload.guessed_character_name}!",
            "guess": payload.guessed_character_name,
            "certainty": 1.0,
            "top_candidates": akinator_instance._get_top_candidates(5)
        })
    else:
        game_state_response = akinator_instance.process_mistaken_guess(payload.guessed_character_name)
        await save_akinator_state(payload.session_id, akinator_instance, pool)
        return JSONResponse(content={"session_id": str(payload.session_id), **game_state_response})