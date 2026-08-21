from datetime import date

from django.utils import timezone

from ..models import TrafficPeriodState


def traffic_requirements(as_of_date=None, start_year=2026):
    as_of_date = as_of_date or timezone.localdate()
    months = []
    cursor = date(start_year, 1, 1)
    current = as_of_date.replace(day=1)
    while cursor <= current:
        months.append(cursor)
        cursor = date(cursor.year + (1 if cursor.month == 12 else 0), 1 if cursor.month == 12 else cursor.month + 1, 1)
    states = {(row.source, row.month): row for row in TrafficPeriodState.objects.all()}
    result = []
    for source in (TrafficPeriodState.Source.SHOPEE, TrafficPeriodState.Source.TIKTOK):
        for month in months:
            state = states.get((source, month))
            if state and state.is_complete:
                continue
            result.append(
                {
                    "source": source,
                    "month": month,
                    "reason": "Belum pernah diimpor" if not state or not state.last_successful_import_at else "Periode belum ditandai complete",
                    "last_import": state.last_successful_import_at if state else None,
                    "last_data_end": state.last_data_end if state else None,
                    "is_current": month == current,
                }
            )
    return result
