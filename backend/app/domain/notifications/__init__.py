"""Email notifications: a transactional outbox and a background sender.

Triggers (triggers.py) write an email_outbox row in the same transaction as the change, so
an email exists if and only if the change committed. reminders.py queues the 24-hour
booking reminder; sender.py delivers over SMTP (Mailpit locally); worker.py runs both on
a timer inside the backend when EMAIL_WORKER_ENABLED is set.
"""
