import os
import json
import uuid
import ssl
from typing import Dict, Any, Optional

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse # Changed from HTMLResponse for root
from fastapi.templating import Jinja2Templates # Keep if you have other templates, but not for root
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
from fastapi.middleware.cors import CORSMiddleware # Import CORS

from algorithm import Akinator

# --- Configuration ---
class Settings(BaseSettings):
    # DATABASE_URL will be pulled from environment variables
    database_url: str = os.environ.get("DATABASE_URL", "postgresql://user:password@localhost:5432/whodatdev_db")
    dataset_path: str = "data/characters_data.json"
    questions_path: str = "data/questions.json"

    class Config:
        env_file = ".env" # For local development
        env_file_encoding = 'utf-8'

settings = Settings()
app = FastAPI()

origins = [
    "https://whodatdev-tau.vercel.app",  # Default Next.js dev port
    "http://localhost:3000",
    # "https://your-deployed-frontend.com", # For production
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # Allows specific origins
    allow_credentials=True, # Allows cookies to be included in requests
    allow_methods=["*"],    # Allows all standard HTTP methods
    allow_headers=["*"],    # Allows all headers
)

# --- Database Connection Pool ---
DB_POOL: Optional[asyncpg.Pool] = None
async def get_db_pool() -> asyncpg.Pool:
    global DB_POOL
    if DB_POOL is None:
        try:
            ssl_context = None
            # Enable SSL for hosted databases like Render
            if "localhost" not in settings.database_url and "127.0.0.1" not in settings.database_url:
                ssl_context = ssl.create_default_context()

            DB_POOL = await asyncpg.create_pool(
                settings.database_url,
                ssl=ssl_context
            )
            
            # Create table if it doesn't exist
            async with DB_POOL.acquire() as connection:
                await connection.execute("""
                    CREATE TABLE IF NOT EXISTS game_sessions (
                        session_id UUID PRIMARY KEY,
                        akinator_state JSONB NOT NULL,
                        last_accessed TIMESTAMPTZ DEFAULT NOW() NOT NULL
                    );
                """)
            
                await connection.execute("""
                    CREATE INDEX IF NOT EXISTS idx_last_accessed ON game_sessions (last_accessed);
                """)
        
        except Exception as e:
            print(f"🔴 Database connection or table creation failed: {e}")
            raise HTTPException(status_code=503, detail="Database service unavailable or setup failed.")
    
    return DB_POOL

# Make Database connection when the app starts
@app.on_event("startup")
async def startup_event():
    await get_db_pool() # Initialize pool and ensure table exists on startup
    print("✅ FastAPI application startup complete. Database pool initialized.")

# Close Database connection when the app starts
@app.on_event("shutdown")
async def shutdown_event():
    if DB_POOL:
        await DB_POOL.close()
        print("ℹ️ Database pool closed.")


# --- Pydantic Models ---
class AnswerPayload(BaseModel):
    session_id: uuid.UUID
    attribute_key: str
    answer: str   # Expected: "yes", "probably not", "probably yes", "no"
    # answer_value: float # Expected: 0.0 (no), 0.25 (probably not), 0.75 (probably yes), 1.0 (yes)

class GuessConfirmationPayload(BaseModel):
    session_id: uuid.UUID
    guessed_character_name: str
    user_confirms_correct: bool

# --- Helper function to retrieve and deserialize Akinator instance ---
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
            raise HTTPException(status_code=500, detail="Failed to load game state. State may be corrupt.")

# --- Helper function to save Akinator instance state ---
async def save_akinator_state(session_id: uuid.UUID, akinator_instance: Akinator, pool: asyncpg.Pool):
    try:
        state_dict = akinator_instance.get_state()
        state_json = json.dumps(state_dict)
        async with pool.acquire() as connection:
            await connection.execute(
                """
                UPDATE game_sessions SET akinator_state = $1, last_accessed = NOW()
                WHERE session_id = $2
                """,
                state_json, session_id
            )
    except Exception as e:
        print(f"🔴 Error serializing Akinator state for session {session_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to save game state.")

# --- API Endpoints ---
@app.get("/")
async def root():
    return JSONResponse(content={"message": "Welcome to the Who Dat Dev? Akinator API!"})

@app.post("/start_game", summary="Start a new game session")
async def start_game_session():
    session_id = uuid.uuid4()
    pool = await get_db_pool()

    try:
        akinator_instance = Akinator(
            dataset_path=settings.dataset_path,
            questions_path=settings.questions_path
        )
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail=f"Dataset not found. Check paths: '{settings.dataset_path}'.")
    except ValueError as e: # Catch other init errors from Akinator
        raise HTTPException(status_code=500, detail=f"Failed to initialize Akinator logic: {str(e)}")

    initial_game_response = akinator_instance.start_game()

    try:
        state_dict = akinator_instance.get_state()
        state_json = json.dumps(state_dict)
        async with pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO game_sessions (session_id, akinator_state, last_accessed)
                VALUES ($1, $2, NOW())
                """,
                session_id, state_json
            )
    except Exception as e:
        print(f"🔴 Error inserting new game session {session_id} into DB: {e}")
        raise HTTPException(status_code=500, detail="Failed to save initial game state to database.")

    return JSONResponse(content={"session_id": str(session_id), **initial_game_response})

@app.post("/questions", summary="While playing the game")
async def submit_answer(payload: AnswerPayload):
    pool = await get_db_pool()
    akinator_instance = await get_akinator_instance(payload.session_id, pool)

    valid_answers = {"no": 0.0, "probably no": 0.25, "probably yes": 0.75, "yes": 1.0}

    if payload.answer.lower() not in valid_answers.keys():
        raise HTTPException(status_code=400, detail=f"Invalid answer. Expected one of {valid_answers.keys()}.")
    game_state_response = akinator_instance.process_answer(payload.attribute_key, valid_answers[payload.answer.lower()])

    # If frontend returns a floating point value instead of string
    # if payload.answer_value not in valid_answers.values():
    #     raise HTTPException(status_code=400, detail=f"Invalid answer value. Expected one of {valid_answers}.")
    # game_state_response = akinator_instance.process_answer(payload.attribute_key, payload.answer_value)
    
    await save_akinator_state(payload.session_id, akinator_instance, pool)

    return JSONResponse(content={"session_id": str(payload.session_id), **game_state_response})

@app.post("/confirm_guess", summary="Confirms or denies the backend's guess")
async def confirm_akinator_guess(payload: GuessConfirmationPayload):
    pool = await get_db_pool()
    akinator_instance = await get_akinator_instance(payload.session_id, pool)

    response_data: Dict[str, Any]
    if payload.user_confirms_correct:
        # Game won, clean up session
        async with pool.acquire() as connection:
            await connection.execute("DELETE FROM game_sessions WHERE session_id = $1", payload.session_id)
        
        response_data = {
            "session_id": str(payload.session_id),
            "status": "finished_won", # This is the "positive result"
            "message": f"🎉 Great! I knew it was {payload.guessed_character_name}!",
            "guess": payload.guessed_character_name,
            "certainty": 1.0, # Or actual certainty if available from akinator_instance
            "top_candidates": akinator_instance._get_top_candidates(5) # Assumes this method exists
        }
    else:
        # Akinator was wrong, continue game by processing mistaken guess
        game_state_response = akinator_instance.process_mistaken_guess(payload.guessed_character_name)
        await save_akinator_state(payload.session_id, akinator_instance, pool)
        response_data = {"session_id": str(payload.session_id), **game_state_response}

    return JSONResponse(content=response_data)

# To run locally (example with Uvicorn):
# 1. Create a .env file with your DATABASE_URL.
#    Example .env content:

#    DATABASE_URL="postgresql://your_user:your_password@your_host:your_port/your_db"
