from fastapi import FastAPI, APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field
from typing import Optional, List
from pathlib import Path
from datetime import datetime, timezone, timedelta
import os, uuid, secrets, logging, json, asyncio

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")
mongo_url = os.environ["MONGO_URL"]
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ["DB_NAME"]]
app = FastAPI(title="White Oak Reservation API", version="1.0.0")
api = APIRouter(prefix="/api")
logger = logging.getLogger("white-oak")

RESTAURANT = {"restaurant_id":"restaurant_white_oak", "name":"White Oak Restaurant", "address":"3/5, Desh Bandhu Gupta Rd, Bazar Sangatrashan, Multani Dhanda, Sadar Bazar, New Delhi, Delhi 110055, India", "opening_time":"11:00", "closing_time":"00:00", "timezone":"Asia/Kolkata", "duration_minutes":90, "buffer_minutes":15, "max_party_size":10}
MENU = [
 {"name":"Veg Special Thali","category":"Thalis","price":399,"veg":True,"popular":True,"description":"A generous spread of comforting North Indian favourites."},
 {"name":"Non Veg Thali","category":"Thalis","price":499,"veg":False,"popular":True,"description":"A hearty selection of chef-led Indian flavours."},
 {"name":"Canteen Style Chili Paneer","category":"Starters","price":289,"veg":True,"popular":True,"description":"Crisp paneer in a bright, smoky sauce."},
 {"name":"Chicken Momos Crunchy Non Veg","category":"Starters","price":329,"veg":False,"popular":True,"description":"Crunchy dumplings with a warming house dip."},
 {"name":"Veg Darjeeling Momos","category":"Starters","price":249,"veg":True,"popular":False,"description":"Delicate steamed dumplings, full of garden vegetables."},
 {"name":"Mexican Crispy Salad","category":"Starters","price":269,"veg":True,"popular":False,"description":"Fresh, crisp and dressed with a lively lime finish."},
 {"name":"Hot and Sour Soup","category":"Soups","price":199,"veg":True,"popular":False,"description":"A fragrant, peppery classic."},
 {"name":"Malai Kofta","category":"Main Course","price":349,"veg":True,"popular":True,"description":"Silken kofta in a mellow, creamy gravy."},
 {"name":"Yellow Dal Tadka","category":"Main Course","price":229,"veg":True,"popular":True,"description":"Slow-simmered lentils finished with a fragrant tadka."},
 {"name":"Thai Curry","category":"Main Course","price":399,"veg":True,"popular":False,"description":"Aromatic curry with a gentle chilli warmth."},
 {"name":"Spicy Arrabiata","category":"Continental","price":329,"veg":True,"popular":False,"description":"Pasta, tomato and chilli with a bold finish."},
 {"name":"Jeera Rice","category":"Rice","price":189,"veg":True,"popular":False,"description":"Basmati rice scented with toasted cumin."},
 {"name":"Egg Fried Rice","category":"Rice","price":249,"veg":False,"popular":False,"description":"Wok-tossed rice with egg and spring onion."},
 {"name":"Laccha Paratha","category":"Breads","price":99,"veg":True,"popular":False,"description":"Plain butter or Lal Mirch."},
 {"name":"Mint Mojito","category":"Beverages","price":199,"veg":True,"popular":True,"description":"Fresh mint, citrus and a cool sparkle."},
 {"name":"Smash Martini","category":"Cocktails","price":349,"veg":True,"popular":True,"description":"A clean, polished bar favourite."},
]
TABLES = [{"table_id":f"table_{i:02d}","table_number":f"{i:02d}","capacity":cap,"section":section,"status":"active","notes":""} for i,(cap,section) in enumerate([(2,"Main Dining"),(2,"Main Dining"),(2,"Window"),(4,"Main Dining"),(4,"Main Dining"),(4,"Fireplace"),(4,"Fireplace"),(6,"Lounge"),(6,"Lounge"),(8,"Private Room"),(8,"Private Room"),(10,"Private Room")],1)]

class AvailabilityRequest(BaseModel):
    date: str
    guests: int = Field(ge=1, le=10)
    preferred_time: Optional[str] = None
class ReservationCreate(BaseModel):
    date: str
    start_time: str
    number_of_guests: int = Field(ge=1, le=10)
    customer_name: str = Field(min_length=2, max_length=80)
    customer_whatsapp: str = Field(min_length=8, max_length=20)
    table_id: Optional[str] = None
    notes: Optional[str] = ""
class ReservationUpdate(BaseModel):
    date: Optional[str] = None; start_time: Optional[str] = None; number_of_guests: Optional[int] = None; status: Optional[str] = None; notes: Optional[str] = None
class LookupRequest(BaseModel):
    booking_code: Optional[str] = None; whatsapp: Optional[str] = None
class ChatRequest(BaseModel):
    message: str
    history: List[dict] = []
class SessionRequest(BaseModel): session_id: str

def clean(doc):
    if not doc: return None
    doc.pop("_id", None)
    return doc
def parse_minutes(value):
    h,m = [int(x) for x in value.split(":")]; return h*60+m
def slot_times():
    return [f"{h:02d}:{m:02d}" for h in range(11,24) for m in (0,30)] + ["00:00"]
def slot_key(date, start, table_id): return f"{date}:{start}:{table_id}"
async def ensure_seed():
    if await db.restaurants.count_documents({}) == 0: await db.restaurants.insert_one(RESTAURANT.copy())
    if await db.tables.count_documents({}) == 0: await db.tables.insert_many(TABLES)
    if await db.menu.count_documents({}) == 0: await db.menu.insert_many([dict(x, item_id=f"menu_{uuid.uuid4().hex[:10]}") for x in MENU])
    await db.reservation_locks.create_index("key", unique=True)
    await db.reservations.create_index([("date",1),("start_time",1),("status",1)])

async def busy_tables(date, start, exclude_reservation_id=None):
    start_m = parse_minutes(start)
    end_m = start_m + RESTAURANT["duration_minutes"] + RESTAURANT["buffer_minutes"]
    query={"date":date,"status":{"$in":["pending","confirmed"]}}
    if exclude_reservation_id: query["reservation_id"]={"$ne":exclude_reservation_id}
    reservations = await db.reservations.find(query,{"_id":0}).to_list(1000)
    return {r["table_id"] for r in reservations if parse_minutes(r["start_time"]) < end_m and start_m < parse_minutes(r["end_time"])}
async def available_for(date, guests, preferred=None, exclude_reservation_id=None):
    if guests > RESTAURANT["max_party_size"]: return []
    now = datetime.now(timezone.utc) + timedelta(hours=2)
    result=[]
    for start in slot_times():
        if date == now.strftime("%Y-%m-%d") and parse_minutes(start) < now.astimezone().hour*60+now.minute: continue
        busy = await busy_tables(date,start,exclude_reservation_id)
        tables = [t for t in TABLES if t["capacity"] >= guests and t["status"] == "active" and t["table_id"] not in busy]
        if tables: result.append({"time":start,"label":datetime.strptime(start,"%H:%M").strftime("%-I:%M %p"),"table_id":tables[0]["table_id"],"capacity":tables[0]["capacity"]})
    return result

@api.get("/")
async def root(): return {"message":"White Oak Reservation API","status":"ok"}
@api.get("/restaurant")
async def restaurant(): return RESTAURANT
@api.get("/menu")
async def menu(): return await db.menu.find({}, {"_id":0}).to_list(1000)
@api.get("/tables")
async def tables(): return await db.tables.find({}, {"_id":0}).to_list(100)
@api.post("/availability")
async def availability(payload: AvailabilityRequest):
    slots = await available_for(payload.date,payload.guests,payload.preferred_time)
    return {"date":payload.date,"guests":payload.guests,"slots":slots,"alternatives":slots[:3] if payload.preferred_time and not any(s["time"]==payload.preferred_time for s in slots) else []}
@api.post("/reservations")
async def create_reservation(payload: ReservationCreate):
    candidates = await available_for(payload.date,payload.number_of_guests,payload.start_time)
    choice = next((x for x in candidates if x["time"] == payload.start_time and (not payload.table_id or x["table_id"]==payload.table_id)),None)
    if not choice: raise HTTPException(409,"That time is no longer available. Please choose another slot.")
    table_id=choice["table_id"]; code=f"WO-{secrets.randbelow(90000)+10000}"; now=datetime.now(timezone.utc).isoformat(); end=(datetime.strptime(payload.start_time,"%H:%M")+timedelta(minutes=90)).strftime("%H:%M")
    lock={"key":slot_key(payload.date,payload.start_time,table_id),"created_at":now,"code":code}
    try: await db.reservation_locks.insert_one(lock)
    except Exception: raise HTTPException(409,"That table was just booked. Please choose another slot.")
    doc={"reservation_id":f"res_{uuid.uuid4().hex[:12]}","confirmation_code":code,"restaurant_id":RESTAURANT["restaurant_id"],"customer_name":payload.customer_name,"customer_whatsapp":payload.customer_whatsapp,"date":payload.date,"start_time":payload.start_time,"end_time":end,"number_of_guests":payload.number_of_guests,"table_id":table_id,"status":"confirmed","notes":payload.notes or "","created_at":now,"updated_at":now,"notification_status":"queued_mock"}
    await db.reservations.insert_one(doc)
    safe_reservation = {key: value for key, value in doc.items() if key != "_id"}
    return {"reservation":safe_reservation,"whatsapp":{"status":"queued_mock","message":"WhatsApp delivery is ready for Meta credentials and currently disabled."}}
@api.post("/reservations/lookup")
async def lookup(payload: LookupRequest):
    query={}
    if payload.booking_code: query["confirmation_code"]=payload.booking_code.upper()
    elif payload.whatsapp: query["customer_whatsapp"]=payload.whatsapp
    else: raise HTTPException(400,"Enter a booking code or WhatsApp number")
    doc=await db.reservations.find_one(query,{"_id":0},sort=[("date",-1),("start_time",-1)])
    if not doc: raise HTTPException(404,"No reservation found")
    return doc
@api.patch("/reservations/{reservation_id}")
async def update_reservation(reservation_id: str, payload: ReservationUpdate):
    current=await db.reservations.find_one({"reservation_id":reservation_id},{"_id":0})
    if not current: raise HTTPException(404,"Reservation not found")
    updates={k:v for k,v in payload.model_dump().items() if v is not None}; updates["updated_at"]=datetime.now(timezone.utc).isoformat()
    target_date=updates.get("date",current["date"]); target_time=updates.get("start_time",current["start_time"]); target_guests=updates.get("number_of_guests",current["number_of_guests"])
    if target_date != current["date"] or target_time != current["start_time"] or target_guests != current["number_of_guests"]:
        target_start=parse_minutes(target_time); target_end=target_start+RESTAURANT["duration_minutes"]+RESTAURANT["buffer_minutes"]
        conflicts=await db.reservations.find({"date":target_date,"status":{"$in":["pending","confirmed"]},"reservation_id":{"$ne":reservation_id}},{"_id":0}).to_list(1000)
        if any(parse_minutes(row["start_time"]) < target_end and target_start < parse_minutes(row["end_time"]) for row in conflicts):
            raise HTTPException(409,"That new time overlaps another reservation. Please choose another slot.")
        options=await available_for(target_date,target_guests,exclude_reservation_id=reservation_id)
        if not any(slot["time"] == target_time and slot["table_id"] == current["table_id"] for slot in options):
            raise HTTPException(409,"That new time is no longer available. Please choose another slot.")
        old_key=slot_key(current["date"],current["start_time"],current["table_id"]); new_key=slot_key(target_date,target_time,current["table_id"])
        if new_key != old_key:
            try:
                await db.reservation_locks.insert_one({"key":new_key,"created_at":updates["updated_at"],"code":current["confirmation_code"]})
            except Exception:
                raise HTTPException(409,"That table was just booked. Please choose another slot.")
            await db.reservation_locks.delete_one({"key":old_key})
    await db.reservations.update_one({"reservation_id":reservation_id},{"$set":updates})
    return clean({**current,**updates})
@api.delete("/reservations/{reservation_id}")
async def cancel_reservation(reservation_id: str):
    result=await db.reservations.update_one({"reservation_id":reservation_id},{"$set":{"status":"cancelled","updated_at":datetime.now(timezone.utc).isoformat()}})
    if not result.matched_count: raise HTTPException(404,"Reservation not found")
    return {"status":"cancelled","notification":"queued_mock"}
@api.get("/admin/summary")
async def admin_summary(request: Request):
    await current_user(request)
    today=datetime.now().strftime("%Y-%m-%d"); all_res=await db.reservations.find({}, {"_id":0}).to_list(1000)
    return {"today":len([r for r in all_res if r["date"]==today and r["status"]!="cancelled"]),"upcoming":len([r for r in all_res if r["status"]=="confirmed"]),"confirmed":len([r for r in all_res if r["status"]=="confirmed"]),"cancelled":len([r for r in all_res if r["status"]=="cancelled"]),"customers":len(set(r["customer_whatsapp"] for r in all_res)),"reservations":all_res}
@api.post("/auth/session")
async def auth_session(payload: SessionRequest, response: Response):
    import requests
    data=requests.get("https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data",headers={"X-Session-ID":payload.session_id},timeout=15).json()
    user={"user_id":f"user_{uuid.uuid4().hex[:12]}","email":data["email"],"name":data.get("name","White Oak Admin"),"picture":data.get("picture","")}
    existing=await db.users.find_one({"email":user["email"]},{"_id":0}); user=existing or user
    if not existing: await db.users.insert_one(user)
    await db.user_sessions.insert_one({"session_token":data["session_token"],"user_id":user["user_id"],"expires_at":(datetime.now(timezone.utc)+timedelta(days=7)).isoformat()})
    response.set_cookie("session_token",data["session_token"],httponly=True,secure=True,samesite="none",path="/")
    return {key: value for key, value in user.items() if key != "_id"}
async def current_user(request: Request):
    token=request.cookies.get("session_token") or request.headers.get("Authorization","").replace("Bearer ","")
    session=await db.user_sessions.find_one({"session_token":token},{"_id":0})
    if not session: raise HTTPException(401,"Not authenticated")
    user=await db.users.find_one({"user_id":session["user_id"]},{"_id":0}); return user
@api.get("/auth/me")
async def auth_me(request: Request): return await current_user(request)
@api.post("/auth/logout")
async def logout(request: Request,response: Response):
    token=request.cookies.get("session_token")
    if token: await db.user_sessions.delete_one({"session_token":token})
    response.delete_cookie("session_token",path="/"); return {"ok":True}
@api.post("/ai/chat")
async def ai_chat(payload: ChatRequest):
    key=os.environ.get("EMERGENT_LLM_KEY")
    if not key: return {"reply":"I can help with hours, menu, and reservations. AI service is not configured yet."}
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
        chat=LlmChat(api_key=key,session_id=f"white-oak-{uuid.uuid4().hex}",system_message="You are White Oak AI, a warm concise booking concierge. Never claim a reservation is confirmed. Ask for missing date, time, guests, name and WhatsApp. Restaurant is open 11 AM to midnight in New Delhi. Use the booking API for real availability.").with_model("gemini","gemini-3-flash-preview")
        response=await chat.send_message(UserMessage(text=payload.message))
        return {"reply":response.content}
    except Exception as exc:
        logger.warning("AI unavailable: %s",exc); return {"reply":"I’m ready to help with your table. Please use the booking form for live availability, or try me again in a moment."}
@api.post("/notifications/whatsapp")
async def whatsapp_status(): return {"provider":"Meta WhatsApp Cloud API","enabled":False,"status":"mocked","message":"Adapter is ready; add Meta credentials and enable delivery when ready."}

app.include_router(api)
app.add_middleware(CORSMiddleware,allow_credentials=True,allow_origins=os.environ.get("CORS_ORIGINS","*").split(","),allow_methods=["*"],allow_headers=["*"])
@app.on_event("startup")
async def startup(): await ensure_seed()
@app.on_event("shutdown")
async def shutdown(): client.close()