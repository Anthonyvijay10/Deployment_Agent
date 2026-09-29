from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Todo Service", version="1.0.0")

class TodoIn(BaseModel):
    title: str
    done: bool = False

class Todo(TodoIn):
    id: int

_db: dict[int, Todo] = {}
_next_id = 1


@app.get("/")
def root():
    return {"message": "Hello from the containerized FastAPI service!"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/todos", response_model=list[Todo])
def list_todos():
    return list(_db.values())


@app.post("/todos", response_model=Todo, status_code=201)
def create_todo(item: TodoIn):
    global _next_id
    todo = Todo(id=_next_id, **item.model_dump())
    _db[_next_id] = todo
    _next_id += 1
    return todo


@app.get("/todos/{todo_id}", response_model=Todo)
def get_todo(todo_id: int):
    if todo_id not in _db:
        raise HTTPException(status_code=404, detail="Todo not found")
    return _db[todo_id]


@app.delete("/todos/{todo_id}", status_code=204)
def delete_todo(todo_id: int):
    if _db.pop(todo_id, None) is None:
        raise HTTPException(status_code=404, detail="Todo not found")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000)
