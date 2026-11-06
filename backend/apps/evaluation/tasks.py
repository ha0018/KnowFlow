from celery import shared_task


@shared_task
def evaluate(run_id):
    from .services import perform_evaluation

    perform_evaluation(run_id)
