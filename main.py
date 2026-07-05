import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)

from agents.watcher import WatcherAgent

print("""
██╗  ██╗██████╗  ██████╗ ███╗   ██╗ ██████╗ ███████╗
██║ ██╔╝██╔══██╗██╔═══██╗████╗  ██║██╔═══██╗██╔════╝
█████╔╝ ██████╔╝██║   ██║██╔██╗ ██║██║   ██║███████╗
██╔═██╗ ██╔══██╗██║   ██║██║╚██╗██║██║   ██║╚════██║
██║  ██╗██║  ██║╚██████╔╝██║ ╚████║╚██████╔╝███████║
╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚═══╝ ╚═════╝ ╚══════╝
Self-Evolving Knowledge Infrastructure
""")

print("Starting KRONOS Watcher...")
print("Drop PDFs into data/inbox/ to process them automatically")
print("Press Ctrl+C to stop\n")
import atexit
from utils.quota import groq_quota

def log_quota_on_exit():
    groq_quota.log_stats()

atexit.register(log_quota_on_exit)
watcher = WatcherAgent()
watcher.start()