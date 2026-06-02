from app.database import SessionLocal, engine, Base
from app.models import User
from app.auth import get_password_hash

# Ensure tables exist
Base.metadata.create_all(bind=engine)

def create_break_glass_admin():
    db = SessionLocal()
    
    admin_email = "admin@redtape.local"
    admin_password = "SuperSecretPassword123!" # You will type this on the login page
    
    # Check if exists
    existing = db.query(User).filter(User.email == admin_email).first()
    if existing:
        print("Admin already exists!")
        return

    admin_user = User(
        email=admin_email,
        name="Local Administrator",
        role="admin",
        is_local=True,
        hashed_password=get_password_hash(admin_password)
    )
    
    db.add(admin_user)
    db.commit()
    print(f"Success! Break-glass admin created. Login at /local-login with {admin_email}")
    db.close()

if __name__ == "__main__":
    create_break_glass_admin()