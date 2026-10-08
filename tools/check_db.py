import sqlite3
conn = sqlite3.connect('auction_data.db')
# Check users table schema
cols = conn.execute('PRAGMA table_info(users)').fetchall()
print('users columns:', [c[1] for c in cols])
# Check user tier
user = conn.execute("SELECT id, username, tier, role FROM users WHERE username='admin'").fetchone()
if user:
    print(f'admin: id={user[0]}, tier={user[1]}, role={user[2]}')
# Check promo codes
promos = conn.execute('SELECT * FROM promo_codes').fetchall()
print(f'promos ({len(promos)}):', promos[:3])
conn.close()
