from celery import shared_task


@shared_task(time_limit=7200, soft_time_limit=7100)
def evaluate(run_id):
    from .services import perform_evaluation

    perform_evaluation(run_id)
