"""稳油控水协同服务向 API 和 CLI 暴露的稳定错误。"""


class WaterControlError(RuntimeError):
    code = "water_control_error"
    status = 400


class NotFound(WaterControlError):
    code = "not_found"
    status = 404


class Conflict(WaterControlError):
    code = "conflict"
    status = 409


class Forbidden(WaterControlError):
    code = "forbidden"
    status = 403


class InvalidState(WaterControlError):
    code = "invalid_state"
    status = 409


class ValidationFailed(WaterControlError):
    code = "validation_failed"
    status = 422
