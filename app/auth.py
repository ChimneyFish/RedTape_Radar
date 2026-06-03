from fastapi import Request, Depends, HTTPException, Security
from sqlalchemy.orm import Session
from fastapi_azure_auth import SingleTenantAzureAuthorizationCodeBearer
from passlib.context import CryptContext
from jose import jwt
from datetime import datetime
from .models import get_db, User

azure_scheme = SingleTenantAzureAuthorizationCodeBearer(
    app_client_id="00000000-0000-0000-0000-000000000000",
    tenant_id="00000000-0000-0000-0000-000000000000",
    scopes={"api://00000000-0000-0000-0000-000000000000/user_impersonation": "Access API"}
)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
SECRET_KEY = "DEPLOYMENT_SECRET_KEY_REPLACE_LATER"
ALGORITHM = "HS256"

def verify_password(plain_password, hashed_password): return pwd_context.verify(plain_password, hashed_password)
def get_password_hash(password): return pwd_context.hash(password)
def create_local_token(email: str): return jwt.encode({"sub": email, "type": "local_admin"}, SECRET_KEY, algorithm=ALGORITHM)

async def get_current_user(request: Request, db: Session = Depends(get_db)):
    auth_header = request.headers.get('Authorization')
    if auth_header and auth_header.startswith('Bearer '):
        try:
            token_payload = await azure_scheme(request)
            email = token_payload.claims.get('preferred_username') or token_payload.claims.get('upn')
            if email:
                user = db.query(User).filter(User.email == email, User.is_active == True).first()
                if not user:
                    user = User(email=email, name=token_payload.claims.get('name', 'SSO User'), role="read_only")
                    db.add(user)
                    db.commit()
                    db.refresh(user)
                return user
        except Exception: pass 

    local_token = request.cookies.get("local_admin_session")
    if local_token:
        try:
            payload = jwt.decode(local_token, SECRET_KEY, algorithms=[ALGORITHM])
            email = payload.get("sub")
            user = db.query(User).filter(User.email == email, User.is_local == True, User.is_active == True).first()
            if user: return user
        except Exception: pass

    raise HTTPException(status_code=401, detail="Authentication required.")

async def require_admin(current_user: User = Depends(get_current_user)):
    if current_user.role != 'admin': raise HTTPException(status_code=403, detail="Admin required.")
    return current_user