"""Route declarations for the ``/rumination`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/rumination/stats', H['rumination_stats'], methods=['GET'])
    router.add_api_route('/rumination/run', H['rumination_run'], methods=['POST'])
    router.add_api_route('/rumination/abstract-heuristics', H['abstract_heuristics'], methods=['POST'])
