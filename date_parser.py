"""
Date parsing utility for handling all human date formats.
Normalizes dates to YYYY-MM-DD format.
"""
import re
from datetime import datetime, timedelta
from typing import Optional, Tuple
import calendar


class DateParser:
    """Parses various human date formats into normalized YYYY-MM-DD format."""
    
    # Month name mappings
    MONTH_NAMES = {
        'january': 1, 'jan': 1,
        'february': 2, 'feb': 2,
        'march': 3, 'mar': 3,
        'april': 4, 'apr': 4,
        'may': 5,
        'june': 6, 'jun': 6,
        'july': 7, 'jul': 7,
        'august': 8, 'aug': 8,
        'september': 9, 'sep': 9, 'sept': 9,
        'october': 10, 'oct': 10,
        'november': 11, 'nov': 11,
        'december': 12, 'dec': 12
    }
    
    # Day name mappings
    DAY_NAMES = {
        'monday': 0, 'mon': 0,
        'tuesday': 1, 'tue': 1, 'tues': 1,
        'wednesday': 2, 'wed': 2,
        'thursday': 3, 'thu': 3, 'thur': 3, 'thurs': 3,
        'friday': 4, 'fri': 4,
        'saturday': 5, 'sat': 5,
        'sunday': 6, 'sun': 6
    }
    
    @staticmethod
    def parse_date(date_string: str, reference_date: Optional[datetime] = None) -> Tuple[Optional[str], Optional[str]]:
        """
        Parse a date string into YYYY-MM-DD format.
        
        Args:
            date_string: The date string to parse
            reference_date: Reference date (defaults to today)
            
        Returns:
            Tuple of (normalized_date, error_message)
            normalized_date: YYYY-MM-DD format or None if parsing failed
            error_message: Error message if parsing failed, None otherwise
        """
        if reference_date is None:
            reference_date = datetime.now()
        
        date_string = date_string.strip().lower()
        
        # Handle relative dates
        if date_string == 'today':
            return reference_date.strftime('%Y-%m-%d'), None
        
        if date_string == 'tomorrow':
            tomorrow = reference_date + timedelta(days=1)
            return tomorrow.strftime('%Y-%m-%d'), None
        
        if date_string == 'day after tomorrow':
            day_after = reference_date + timedelta(days=2)
            return day_after.strftime('%Y-%m-%d'), None
        
        # Handle "next [day]" format
        next_day_match = re.match(r'next\s+(\w+)', date_string)
        if next_day_match:
            day_name = next_day_match.group(1)
            if day_name in DateParser.DAY_NAMES:
                target_weekday = DateParser.DAY_NAMES[day_name]
                days_ahead = target_weekday - reference_date.weekday()
                if days_ahead <= 0:  # If day has passed this week, get next week
                    days_ahead += 7
                target_date = reference_date + timedelta(days=days_ahead)
                return target_date.strftime('%Y-%m-%d'), None
        
        # Remove ordinal suffixes (st, nd, rd, th)
        date_string = re.sub(r'(\d+)(st|nd|rd|th)', r'\1', date_string)
        
        # Pattern 1: "21 January" or "21 Jan"
        pattern1 = re.match(r'^(\d{1,2})\s+(\w+)(?:\s+(\d{4}))?$', date_string)
        if pattern1:
            day = int(pattern1.group(1))
            month_str = pattern1.group(2)
            year = int(pattern1.group(3)) if pattern1.group(3) else reference_date.year
            
            if month_str in DateParser.MONTH_NAMES:
                month = DateParser.MONTH_NAMES[month_str]
                try:
                    parsed_date = datetime(year, month, day)
                    # Only reject if the date is explicitly in the past year
                    # If same year but past, allow it (user might be referring to next year)
                    if parsed_date.date() < reference_date.date() and year < reference_date.year:
                        return None, "That date has already passed. Please provide another date."
                    # If same year but past month/day, check if it's clearly in the past
                    if parsed_date.date() < reference_date.date() and year == reference_date.year:
                        # Only reject if it's more than 30 days in the past (likely a mistake)
                        days_diff = (reference_date.date() - parsed_date.date()).days
                        if days_diff > 30:
                            return None, "That date has already passed. Please provide another date."
                    return parsed_date.strftime('%Y-%m-%d'), None
                except ValueError:
                    return None, "Invalid date. Please provide a valid date."
        
        # Pattern 2: "January 21" or "Jan 21"
        pattern2 = re.match(r'^(\w+)\s+(\d{1,2})(?:\s+(\d{4}))?$', date_string)
        if pattern2:
            month_str = pattern2.group(1)
            day = int(pattern2.group(2))
            year = int(pattern2.group(3)) if pattern2.group(3) else reference_date.year
            
            if month_str in DateParser.MONTH_NAMES:
                month = DateParser.MONTH_NAMES[month_str]
                try:
                    parsed_date = datetime(year, month, day)
                    # Only reject if the date is explicitly in the past year
                    # If same year but past, allow it (user might be referring to next year)
                    if parsed_date.date() < reference_date.date() and year < reference_date.year:
                        return None, "That date has already passed. Please provide another date."
                    # If same year but past month/day, check if it's clearly in the past
                    if parsed_date.date() < reference_date.date() and year == reference_date.year:
                        # Only reject if it's more than 30 days in the past (likely a mistake)
                        days_diff = (reference_date.date() - parsed_date.date()).days
                        if days_diff > 30:
                            return None, "That date has already passed. Please provide another date."
                    return parsed_date.strftime('%Y-%m-%d'), None
                except ValueError:
                    return None, "Invalid date. Please provide a valid date."
        
        # Pattern 3: Just a number (day of current month)
        pattern3 = re.match(r'^(\d{1,2})$', date_string)
        if pattern3:
            day = int(pattern3.group(1))
            year = reference_date.year
            month = reference_date.month
            
            try:
                parsed_date = datetime(year, month, day)
                # If the date has passed in current month, automatically try next month
                if parsed_date.date() < reference_date.date():
                    # Day has passed, try next month
                    if month == 12:
                        month = 1
                        year += 1
                    else:
                        month += 1
                    try:
                        parsed_date = datetime(year, month, day)
                        return parsed_date.strftime('%Y-%m-%d'), None
                    except ValueError:
                        return None, "Invalid date. Please provide a valid date."
                return parsed_date.strftime('%Y-%m-%d'), None
            except ValueError:
                # Day doesn't exist in current month, try next month
                try:
                    if month == 12:
                        month = 1
                        year += 1
                    else:
                        month += 1
                    parsed_date = datetime(year, month, day)
                    return parsed_date.strftime('%Y-%m-%d'), None
                except ValueError:
                    return None, "Invalid date. Please provide a valid date."
        
        # Pattern 4: "DD/MM/YYYY" or "DD-MM-YYYY" or "DD.MM.YYYY"
        pattern4 = re.match(r'^(\d{1,2})[/\-\.](\d{1,2})[/\-\.](\d{4})$', date_string)
        if pattern4:
            day = int(pattern4.group(1))
            month = int(pattern4.group(2))
            year = int(pattern4.group(3))
            try:
                parsed_date = datetime(year, month, day)
                # If date has explicit year and is in the past, reject it
                if parsed_date.date() < reference_date.date():
                    return None, "That date has already passed. Please provide another date."
                return parsed_date.strftime('%Y-%m-%d'), None
            except ValueError:
                return None, "Invalid date. Please provide a valid date."
        
        # Pattern 5: "YYYY-MM-DD" (already normalized)
        pattern5 = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', date_string)
        if pattern5:
            year = int(pattern5.group(1))
            month = int(pattern5.group(2))
            day = int(pattern5.group(3))
            try:
                parsed_date = datetime(year, month, day)
                # If date has explicit year and is in the past, reject it
                if parsed_date.date() < reference_date.date():
                    return None, "That date has already passed. Please provide another date."
                return parsed_date.strftime('%Y-%m-%d'), None
            except ValueError:
                return None, "Invalid date. Please provide a valid date."
        
        return None, "I couldn't understand that date format. Please provide the date in a format like '21 January', 'January 21', '21/01/2024', or 'tomorrow'."
