from typing import Protocol

class UserRepositoryInterface(Protocol):

    def get_by_id(self, id: int): 
        ...