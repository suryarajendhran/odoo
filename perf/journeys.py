"""User journeys measured by the performance benchmark.

A journey is one thing a user does and waits for. Each journey is measured
from its trigger (navigation start for page loads, the click/action for
in-app journeys) until the page is *settled*: the ``ready`` selector matches,
no RPC is in flight and the DOM has not changed for a short quiet period.

``prepare`` runs before every measured iteration and is not measured, it
brings the web client in the state the journey starts from. ``trigger``
performs the user action being measured.

To add a journey: append a ``Journey`` to ``JOURNEYS``, run the benchmark
locally (see perf/README.md) and update the baseline in the same PR.
"""
from collections.abc import Callable
from dataclasses import dataclass

ACTION_READY = ".o_action_manager > *"


def _with_env(page, body, *args):
    """Run ``body`` in the page with the web client ``env`` and ``args`` in scope."""
    return page.evaluate(
        "async (args) => { const env = odoo.__WOWL_DEBUG__.root.env; %s }" % body,
        list(args),
    )


def do_action(page, action, wait=True, **options):
    # when not waiting, the action is triggered and the journey timing
    # (in page) decides when it is done
    body = "const done = env.services.action.doAction(args[0], args[1]);"
    if wait:
        body += " await done;"
    _with_env(page, body, action, options)


def open_record(page, model, res_id):
    do_action(page, {
        'type': 'ir.actions.act_window',
        'res_model': model,
        'res_id': res_id,
        'views': [[False, 'form']],
    }, clearBreadcrumbs=True)


def orm_call(page, model, method, args, kwargs=None):
    return _with_env(page, "return env.services.orm.call(...args);", model, method, args, kwargs or {})


def orm_search(page, model, domain, limit=1):
    return _with_env(page, "return env.services.orm.search(args[0], args[1], {limit: args[2], order: 'id'});",
                     model, domain, limit)


def click(page, selector):
    page.evaluate("(selector) => document.querySelector(selector).click()", selector)


@dataclass
class Journey:
    name: str
    description: str
    ready: str
    # page_load journeys navigate to ``url``, in_app journeys run ``trigger``
    # in an already loaded web client.
    kind: str = 'in_app'
    url: str = '/odoo'
    cold_cache: bool = True
    prepare: Callable | None = None
    trigger: Callable | None = None
    # extra time the DOM must stay unchanged before considering it settled
    quiet_ms: int = 300


# ---------------------------------------------------------------------------
# prepare / trigger steps
# ---------------------------------------------------------------------------

def _prepare_sale_list(page, state):
    do_action(page, 'sale.action_orders', clearBreadcrumbs=True)
    page.wait_for_selector('.o_list_view .o_data_row')


def _prepare_crm_pipeline(page, state):
    do_action(page, 'crm.crm_lead_action_pipeline', clearBreadcrumbs=True)
    page.wait_for_selector('.o_kanban_view .o_kanban_record')


def _trigger_sale_form(page, state):
    click(page, '.o_list_view .o_data_row .o_data_cell')


def _reload_sale_list(page, state):
    # new page load: the channel is not in the client-side store yet, as when
    # a user opens it for the first time in a session
    page.goto(page.url.split('/odoo')[0] + '/odoo/action-sale.action_orders')
    page.wait_for_selector('.o_list_view .o_data_row')


def _trigger_discuss_channel(page, state):
    if 'channel_id' not in state:
        state['channel_id'] = orm_search(page, 'discuss.channel', [('name', '=', 'General')])[0]
    do_action(page, 'mail.action_discuss', wait=False, clearBreadcrumbs=True,
              additionalContext={'active_id': state['channel_id']})


def _prepare_sale_confirm(page, state):
    # confirm a fresh copy of the same draft quotation every iteration, and
    # cancel the copy confirmed by the previous iteration: confirming reserves
    # stock, which changes what the next confirmation does (replenishment,
    # picking merge...). Cancelling restores the stock state so that every
    # iteration does exactly the same work.
    if 'quotation_id' not in state:
        state['quotation_id'] = orm_search(page, 'sale.order', [('state', '=', 'draft')])[0]
    if 'confirmed_id' in state:
        orm_call(page, 'sale.order', 'action_cancel', [[state.pop('confirmed_id')]])
    new_id = orm_call(page, 'sale.order', 'copy', [[state['quotation_id']]])[0]
    state['confirmed_id'] = new_id
    open_record(page, 'sale.order', new_id)
    page.wait_for_selector('.o_form_view button[name="action_confirm"]')


def _trigger_sale_confirm(page, state):
    click(page, '.o_form_view .o_statusbar_buttons button[name="action_confirm"]')


# ---------------------------------------------------------------------------
# journeys
# ---------------------------------------------------------------------------

JOURNEYS = [
    Journey(
        name='webclient_cold_load',
        description="Load /odoo with an empty browser cache (first visit of the day)",
        kind='page_load',
        url='/odoo',
        cold_cache=True,
        ready=ACTION_READY,
    ),
    Journey(
        name='webclient_warm_load',
        description="Reload /odoo with a populated browser cache (F5)",
        kind='page_load',
        url='/odoo',
        cold_cache=False,
        ready=ACTION_READY,
    ),
    Journey(
        name='sale_list_open',
        description="Open the Sales Orders list view from the web client",
        ready='.o_list_view .o_data_row',
        prepare=_prepare_crm_pipeline,
        trigger=lambda page, state: do_action(page, 'sale.action_orders', wait=False, clearBreadcrumbs=True),
    ),
    Journey(
        name='sale_form_open',
        description="Open a sales order form from the list view",
        ready='.o_form_view .o_field_widget[name="order_line"] .o_data_row',
        prepare=_prepare_sale_list,
        trigger=_trigger_sale_form,
    ),
    Journey(
        name='crm_pipeline_open',
        description="Open the CRM pipeline kanban view",
        ready='.o_kanban_view .o_kanban_record',
        prepare=_prepare_sale_list,
        trigger=lambda page, state: do_action(page, 'crm.crm_lead_action_pipeline', wait=False, clearBreadcrumbs=True),
    ),
    Journey(
        name='discuss_channel_open',
        description="Open the General channel in Discuss",
        ready='.o-mail-Discuss .o-mail-Thread .o-mail-Message',
        prepare=_reload_sale_list,
        trigger=_trigger_discuss_channel,
    ),
    Journey(
        # mutates data, keep it last
        name='sale_order_confirm',
        description="Confirm a quotation from its form view",
        ready='.o_form_view .o_statusbar_status button.o_arrow_button_current[data-value="sale"]',
        prepare=_prepare_sale_confirm,
        trigger=_trigger_sale_confirm,
    ),
]

JOURNEYS_BY_NAME = {journey.name: journey for journey in JOURNEYS}
