from fastapi import FastAPI, HTTPException, status, Depends, Request, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import List, Dict, Optional
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
import datetime
import uvicorn
from fastapi.middleware.cors import CORSMiddleware

from pipeline import pipeline
from priority import DBManager
from user import create_user, validate_credentials, get_user_details
from dotenv import load_dotenv
import os

class Prompt(BaseModel):
    prompt: str

class User(BaseModel):
    username: str
    password: str
    email: str

class LoginRequest(BaseModel):
    email: str
    password: str

class Event(BaseModel):
    event_name: str
    start_datetime: datetime.datetime
    end_datetime: datetime.datetime
    event_date: str
    event_flexibility: int
    event_importance: int

class RateLimiter:
    def __init__(self):
        self.requests = {}

    def limit(self, user_id: int, max_requests: int):
        if user_id in self.requests:
            self.requests[user_id]["count"] += 1
        else:
            self.requests[user_id] = {"count": 1, "time": datetime.datetime.now()}

        time_diff = datetime.datetime.now() - self.requests[user_id]["time"]
        if time_diff.days >= 1:
            self.requests[user_id] = {"count": 1, "time": datetime.datetime.now()}
        elif self.requests[user_id]["count"] > max_requests:
            return False

        return True

rate_limiter = RateLimiter()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

load_dotenv()
SECRET_KEY = os.getenv("SECRET_KEY")
API_KEY = os.getenv("API_KEY")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60

async def verify_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

async def get_current_user(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("user")
        if email is None:
            raise credentials_exception
        # Fetch more user details if needed
        user = get_user_details(email)
        if user is None:
            raise credentials_exception
        # Check if token has expired
        expiration = payload.get("exp")
        if expiration is None or datetime.datetime.utcnow() > datetime.datetime.fromtimestamp(expiration):
            raise credentials_exception
        return user
    except JWTError:
        raise credentials_exception

@app.post("/login")
async def login(request: LoginRequest, api_key: str = Depends(verify_api_key)):
    email = request.email
    password = request.password

    if validate_credentials(email, password):
        token = jwt.encode({
            'user': email,
            'exp': datetime.datetime.utcnow() + datetime.timedelta(hours=24)
        }, SECRET_KEY, algorithm=ALGORITHM)
        return {"token": token}
    else:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication failed. Please try again.")

@app.post("/register")
async def register(user: User, api_key: str = Depends(verify_api_key)):
    create_user(user.username, user.password, user.email)
    return {"message": "User created successfully."}
    
@app.post("/create_event")
async def create_event(prompt: Prompt, user_id: int = Depends(get_current_user), api_key: str = Depends(verify_api_key)):
    if not rate_limiter.limit(user_id, 20):
        return JSONResponse(status_code=429, content={"message": "Rate limit exceeded"})

    event_data = prompt.prompt
    event_data = pipeline(event_data, user_id)
    

    if event_data == None:
        return {"message": "Event creation failed"}
    else:
        return {"message": event_data}
    
@app.get("/events")
async def get_events(user_id: int = Depends(get_current_user), api_key: str = Depends(verify_api_key)):
    with DBManager('calendar_app.db') as cursor:
        cursor.execute("SELECT * FROM events WHERE user_id = ?", (user_id,))
        events = cursor.fetchall()
        return events
    
@app.patch("/events/{id}")
async def update_event(id: int, event: Event, user_id: int = Depends(get_current_user), api_key: str = Depends(verify_api_key)):
    update_data = event.dict(exclude_unset=True)

    # Formatting date and datetime fields before updating
    if 'start_datetime' in update_data:
        update_data['start_datetime'] = update_data['start_datetime'].strftime("%Y-%m-%d %H:%M:%S")
    if 'end_datetime' in update_data:
        update_data['end_datetime'] = update_data['end_datetime'].strftime("%Y-%m-%d %H:%M:%S")

    # Check if update_data is empty (no data to update)
    if not update_data:
        return

    set_clause = ", ".join([f"{key} = ?" for key in update_data])
    values = list(update_data.values()) + [id, user_id]

    query = f"UPDATE events SET {set_clause} WHERE id = ? AND user_id = ?"

    with DBManager('calendar_app.db') as cursor:
        cursor.execute(query, values)
        if cursor.rowcount == 0:
            # No rows were updated, likely because the ID was not found.
            return {"message": "No update performed. Check if the event exists and belongs to the user."}
        return


@app.delete("/events/{id}")
async def delete_event(id: int, user_id: int = Depends(get_current_user), api_key: str = Depends(verify_api_key)):
    with DBManager('calendar_app.db') as cursor:
        cursor.execute("DELETE FROM events WHERE id = ? AND user_id = ?", (id, user_id))
        return {"message": "Event deleted successfully."}
    
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
