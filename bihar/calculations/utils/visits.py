def visit_is(action, visit_type):
    """
    for a given action returns whether it's a visit of the type
    """
    return action.updated_unknown_properties.get('last_visit_type', None) == visit_type

def has_visit(case, type):
    """
    returns whether a visit of a type exists in the case
    """
    return len(filter(lambda a: visit_is(a, type), case.actions)) > 0