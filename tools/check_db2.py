import sqlite3
conn = sqlite3.connect('auction_data.db')
# Check who used the promo
user378 = conn.execute("SELECT id, username, tier, role, email FROM users WHERE id=378").fetchone()
print('user 378:', user378)
# Also check total users count
cnt = conn.execute("SELECT COUNT(*), SUM(CASE WHEN tier='premium' THEN 1 ELSE 0 END) FROM users").fetchone()
print(f'total users: {cnt[0]}, premium: {cnt[1]}')
conn.close()
