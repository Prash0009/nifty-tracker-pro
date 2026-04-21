# Nifty Pro Tracker

import time
import random

class NiftyProTracker:
    def __init__(self):
        self.hold_duration = 10 * 60  # in seconds (10 minutes)
        self.pnl = 0
        self.recommendations = []

    def track_pnl(self, profit_loss):
        self.pnl += profit_loss
        print(f"Current P&L: {self.pnl}")

    def hold_updates(self):
        while True:
            self.send_hold_update()
            time.sleep(self.hold_duration)

    def send_hold_update(self):
        # Sample hold indicators with emojis
        indicators = ["✅ Keep Holding!", "⚠️ Consider Selling!", "😊 Good to Go!"]
        print(random.choice(indicators))

    def generate_recommendations(self):
        # Simple recommendation logic
        if self.pnl > 0:
            self.recommendations.append("Hold your position.")
        else:
            self.recommendations.append("Consider exiting for a better opportunity.")
        print(self.recommendations[-1])

# Example usage
if __name__ == '__main__':
    tracker = NiftyProTracker()
    tracker.hold_updates()