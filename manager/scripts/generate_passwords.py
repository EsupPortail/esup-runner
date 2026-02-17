# utils/generate_passwords.py
"""Use this script to generate hashed passwords for the .env file."""

from app.core.passwords import BcryptPasswordContext

# Configuring the hashing context (bcrypt)
pwd_context = BcryptPasswordContext()

# List of plaintext passwords (to be replaced with your own)
passwords = {
    "admin1": "mdp1",
    "admin2": "mdp2",
    "admin3": "mdp3",
}

# Generate hashes
hashed_passwords = {user: pwd_context.hash(pwd) for user, pwd in passwords.items()}

# Display hashes for .env
for user, hashed_pwd in hashed_passwords.items():
    print(f'ADMIN_USERS__{user}="{hashed_pwd}"')
