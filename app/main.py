from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
from uuid import uuid4
from datetime import datetime

from sqlalchemy import (
    create_engine,
    Column,
    String,
    Integer,
    DateTime,
    ForeignKey,
    Boolean,
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship

# ===== DB設定 =====

DATABASE_URL = "sqlite:///./pos.db"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # SQLite用
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


class Order(Base):
    __tablename__ = "orders"

    id = Column(String, primary_key=True, index=True)  # orderId
    ticket_number = Column(String, index=True)
    status = Column(String, index=True, default="open")  # "open" or "done"
    created_at = Column(DateTime, default=datetime.utcnow)
    exported = Column(Boolean, default=False, index=True)
    items = relationship("OrderItem", back_populates="order")


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    order_id = Column(String, ForeignKey("orders.id"))
    base_name = Column(String)
    toppings = Column(String)  # "チーズ,野菜" みたいな文字列
    quantity = Column(Integer)
    unit_price = Column(Integer)  # 単価

    order = relationship("Order", back_populates="items")


Base.metadata.create_all(bind=engine)

# ===== Pydanticスキーマ =====


class OrderItemIn(BaseModel):
    baseName: str
    toppings: List[str]
    quantity: int
    unitPrice: int


class OrderCreate(BaseModel):
    ticketNumber: str
    items: List[OrderItemIn]


class PendingItemOut(BaseModel):
    baseName: str
    toppings: List[str]
    quantity: int
    unitPrice: int
    lineTotal: int


class PendingOrderOut(BaseModel):
    orderId: str
    ticketNumber: str
    timestamp: str
    items: List[PendingItemOut]
    total: int


# ===== FastAPI本体 =====

app = FastAPI()

# フロントから叩けるようにCORS許可
origins = [
    "http://localhost:5173",      # ローカルのVite
    "http://127.0.0.1:5173",
    "https://squad22.netlify.app",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ===== エンドポイント =====

from fastapi import Depends

@app.post("/orders")
def create_order(order: OrderCreate, db=Depends(get_db)):
    """
    新しい注文を登録する
    """
    if not order.items:
        raise HTTPException(status_code=400, detail="items is empty")

    order_id = str(uuid4())
    db_order = Order(
        id=order_id,
        ticket_number=order.ticketNumber,
        status="open",
    )
    db.add(db_order)

    for item in order.items:
        toppings_str = ",".join(item.toppings)
        db_item = OrderItem(
            order_id=order_id,
            base_name=item.baseName,
            toppings=toppings_str,
            quantity=item.quantity,
            unit_price=item.unitPrice,
        )
        db.add(db_item)

    db.commit()
    return {"status": "ok", "orderId": order_id}


@app.get("/orders", response_model=List[PendingOrderOut])
def list_orders(status: str = "open", db=Depends(get_db)):
    """
    未受け渡し一覧取得など:
    GET /orders?status=open
    """
    q = db.query(Order).filter(Order.status == status).order_by(Order.created_at.asc())
    orders = q.all()

    result: List[PendingOrderOut] = []
    for o in orders:
        items_out: List[PendingItemOut] = []
        total = 0
        for it in o.items:
            line_total = it.unit_price * it.quantity
            total += line_total
            items_out.append(
                PendingItemOut(
                    baseName=it.base_name,
                    toppings=it.toppings.split(",") if it.toppings else [],
                    quantity=it.quantity,
                    unitPrice=it.unit_price,
                    lineTotal=line_total,
                )
            )

        result.append(
            PendingOrderOut(
                orderId=o.id,
                ticketNumber=o.ticket_number,
                timestamp=o.created_at.isoformat(),
                items=items_out,
                total=total,
            )
        )

    return result


@app.patch("/orders/{order_id}/close")
def close_order(order_id: str, db=Depends(get_db)):
    """
    受け渡し済みにする
    """
    o = db.query(Order).filter(Order.id == order_id).first()
    if not o:
        raise HTTPException(status_code=404, detail="order not found")

    o.status = "done"
    db.commit()
    return {"status": "ok"}


import requests
import os

SHEETS_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbz1aQS6sk1GIN5dCpdbokJ2iJrCrP7iamXeTop1R5-JelfNrHS3INy4cgdlyRmocZx3/exec"

from fastapi import Depends, HTTPException

@app.post("/export-to-sheets")
def export_to_sheets(db=Depends(get_db)):
    """
    まだ Sheets に送っていない注文だけを Google スプレッドシートへ送信する
    条件：
      - status = "done"
      - exported = False
    """
    # 1. 新規分（未エクスポート & done）のオーダーを取得
    orders = (
        db.query(Order)
        .filter(Order.status == "done", Order.exported == False)  # noqa: E712
        .order_by(Order.created_at.asc())
        .all()
    )

    if not orders:
        return {"status": "ok", "exported": 0}

    rows = []
    for o in orders:
        for it in o.items:
            line_total = it.unit_price * it.quantity
            rows.append({
                "timestamp": o.created_at.isoformat(),
                "ticketNumber": o.ticket_number,
                "orderId": o.id,
                "baseName": it.base_name,
                "toppings": (it.toppings or "").split(",") if it.toppings else [],
                "quantity": it.quantity,
                "unitPrice": it.unit_price,
                "lineTotal": line_total,
                "status": o.status,
            })

    # 2. Apps Script に POST
    try:
        resp = requests.post(
            SHEETS_WEBHOOK_URL,
            json={"rows": rows},
            timeout=20,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Sheets request error: {e}")

    if not resp.ok:
        raise HTTPException(status_code=500, detail=f"Sheets error: {resp.text}")

    # 3. 送信成功したので exported = True に更新
    for o in orders:
        o.exported = True
    db.commit()

    return {"status": "ok", "exported": len(rows)}
