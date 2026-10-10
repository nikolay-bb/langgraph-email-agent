"""Tool prompt templates for Gmail integration."""

# Gmail tools prompt for insertion into agent system prompts
GMAIL_TOOLS_PROMPT = """
1. send_email_tool(email_id, response_text, email_address, additional_recipients) - Reply to the email in its Gmail thread; email_id is the ID shown with the email, and the reply goes to its sender
2. check_calendar_tool(dates) - Check Google Calendar availability for specific dates, each written as DD-MM-YYYY; free time is given between 09:00 and 17:00 UK time
3. schedule_meeting_tool(attendees, title, start_time, end_time, organizer_email, timezone) - Schedule a new meeting and send invites; start_time and end_time are local times written as YYYY-MM-DDTHH:MM:SS with no Z or offset, in timezone (default Europe/London)
4. update_meeting_tool(event_id, start_time, end_time, timezone) - Move an existing meeting to a new time and email the attendees the change; event_id is the id the calendar check lists for the event; times as for schedule_meeting_tool
5. cancel_meeting_tool(event_id) - Cancel an existing meeting and email the attendees; event_id as for update_meeting_tool
6. Question(content) - Ask the user any follow-up questions
7. Done - The work on this email is finished
"""
