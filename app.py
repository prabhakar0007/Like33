from flask import Flask, request, jsonify
import asyncio
import binascii
import aiohttp
import requests
import like_pb2
import like_count_pb2
import uid_generator_pb2
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import urllib3
import threading
import random

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==============================
# CONFIGURATION
# ==============================
# Hardcoded token(s) - add more tokens in this list if needed
TOKENS = [
    {"token": "ac0062d5c17cb8fa49f49594ff2776e2"}   # Your provided token
]

# For profile check (visit token) - same token used, can be different if needed
VISIT_TOKENS = TOKENS.copy()   # Using same token for profile checks

# India server URLs
PROFILE_URL = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
LIKE_URL = "https://client.ind.freefiremobile.com/LikeProfile"
REGION = "IND"

# Batch size (if multiple tokens exist, otherwise ignored)
TOKEN_BATCH_SIZE = 100

# AES key and IV (same as original)
AES_KEY = b'Yg&tc%DEuh6%Zc^8'
AES_IV = b'6oyZDr22E3ychjM%'

# Global state for token rotation (only used if multiple tokens)
current_batch_index = 0
batch_lock = threading.Lock()

# ==============================
# HELPER FUNCTIONS
# ==============================
def encrypt_message(plaintext: bytes) -> str:
    """Encrypt with AES-CBC and return hex string."""
    cipher = AES.new(AES_KEY, AES.MODE_CBC, AES_IV)
    padded = pad(plaintext, AES.block_size)
    encrypted = cipher.encrypt(padded)
    return binascii.hexlify(encrypted).decode('utf-8')

def create_like_protobuf(uid: int, region: str) -> bytes:
    """Create protobuf message for LikeProfile request."""
    msg = like_pb2.like()
    msg.uid = uid
    msg.region = region
    return msg.SerializeToString()

def create_profile_protobuf(uid: int) -> bytes:
    """Create protobuf message for GetPlayerPersonalShow request."""
    msg = uid_generator_pb2.uid_generator()
    msg.krishna_ = uid
    msg.teamXdarks = 1
    return msg.SerializeToString()

def get_next_batch_tokens(all_tokens):
    """Rotating batch selection (for multiple tokens)."""
    if not all_tokens:
        return []
    total = len(all_tokens)
    if total <= TOKEN_BATCH_SIZE:
        return all_tokens.copy()
    global current_batch_index
    with batch_lock:
        start = current_batch_index
        end = start + TOKEN_BATCH_SIZE
        if end > total:
            batch = all_tokens[start:] + all_tokens[:end - total]
        else:
            batch = all_tokens[start:end]
        current_batch_index = (current_batch_index + TOKEN_BATCH_SIZE) % total
        return batch

def get_random_batch_tokens(all_tokens):
    """Random batch selection (for multiple tokens)."""
    if not all_tokens:
        return []
    total = len(all_tokens)
    if total <= TOKEN_BATCH_SIZE:
        return all_tokens.copy()
    return random.sample(all_tokens, TOKEN_BATCH_SIZE)

def get_profile_info(uid: int, token_dict):
    """Fetch profile info (likes count, nickname, etc.) using a token."""
    token = token_dict.get("token")
    if not token:
        return None
    encrypted = encrypt_message(create_profile_protobuf(uid))
    data = bytes.fromhex(encrypted)
    headers = {
        'Authorization': f"Bearer {token}",
        'Content-Type': "application/x-www-form-urlencoded",
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'X-Unity-Version': "2018.4.11f1",
        'Accept-Encoding': "gzip",
    }
    try:
        resp = requests.post(PROFILE_URL, data=data, headers=headers, verify=False, timeout=10)
        resp.raise_for_status()
        info = like_count_pb2.Info()
        info.ParseFromString(resp.content)
        return info
    except Exception as e:
        print(f"Profile check error for token {token[:8]}...: {e}")
        return None

async def send_single_like(uid: int, region: str, token_dict):
    """Send one like request asynchronously."""
    token = token_dict.get("token")
    if not token:
        return 999
    encrypted = encrypt_message(create_like_protobuf(uid, region))
    data = bytes.fromhex(encrypted)
    headers = {
        'Authorization': f"Bearer {token}",
        'Content-Type': "application/x-www-form-urlencoded",
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'X-Unity-Version': "2018.4.11f1",
        'Accept-Encoding': "gzip",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(LIKE_URL, data=data, headers=headers, timeout=10) as resp:
                return resp.status
    except asyncio.TimeoutError:
        print(f"Timeout for token {token[:8]}...")
        return 998
    except Exception as e:
        print(f"Error for token {token[:8]}...: {e}")
        return 997

async def send_batch_likes(uid, region, token_batch):
    """Send likes using a batch of tokens."""
    if not token_batch:
        return []
    tasks = [send_single_like(uid, region, t) for t in token_batch]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    success = sum(1 for r in results if isinstance(r, int) and r == 200)
    print(f"Batch: sent {len(token_batch)} likes, success: {success}")
    return results

# ==============================
# FLASK APP
# ==============================
app = Flask(__name__)

@app.route('/like', methods=['GET'])
def like_profile():
    """Main endpoint to send likes to a given UID."""
    uid_param = request.args.get("uid")
    use_random = request.args.get("random", "false").lower() == "true"

    if not uid_param:
        return jsonify({"error": "Missing uid parameter"}), 400

    try:
        uid = int(uid_param)
    except ValueError:
        return jsonify({"error": "UID must be an integer"}), 400

    # Use first token from VISIT_TOKENS for profile checks
    visit_token = VISIT_TOKENS[0] if VISIT_TOKENS else None
    if not visit_token:
        return jsonify({"error": "No visit token available"}), 500

    # Get likes BEFORE
    before_info = get_profile_info(uid, visit_token)
    before_likes = 0
    nickname = "N/A"
    if before_info and before_info.HasField('AccountInfo'):
        before_likes = before_info.AccountInfo.Likes
        if before_info.AccountInfo.PlayerNickname:
            nickname = before_info.AccountInfo.PlayerNickname

    print(f"UID {uid}: before likes = {before_likes}")

    # Decide which tokens to use for likes
    if len(TOKENS) == 0:
        return jsonify({"error": "No like tokens available"}), 500

    if use_random:
        token_batch = get_random_batch_tokens(TOKENS)
    else:
        token_batch = get_next_batch_tokens(TOKENS)

    # Send likes
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(send_batch_likes(uid, REGION, token_batch))
    finally:
        loop.close()

    # Get likes AFTER
    after_info = get_profile_info(uid, visit_token)
    after_likes = before_likes
    if after_info and after_info.HasField('AccountInfo'):
        after_likes = after_info.AccountInfo.Likes

    increment = after_likes - before_likes

    return jsonify({
        "UID": uid,
        "Nickname": nickname,
        "LikesBefore": before_likes,
        "LikesAfter": after_likes,
        "LikesGiven": increment,
        "Status": "success" if increment > 0 else "no_change",
        "Mode": "random" if use_random else "rotating",
        "TokensUsed": len(token_batch)
    })

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    return jsonify({
        "status": "ok",
        "server": REGION,
        "tokens_available": len(TOKENS),
        "visit_tokens": len(VISIT_TOKENS)
    })

if __name__ == '__main__':
    # Run on all interfaces, port 5001
    app.run(host='0.0.0.0', port=5001, debug=False, use_reloader=False)