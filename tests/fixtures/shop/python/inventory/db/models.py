from sqlalchemy import Column, ForeignKey, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Warehouse(Base):
    __tablename__ = "warehouses"
    id = Column(Integer, primary_key=True)
    name = Column(String(80))


class Item(Base):
    __tablename__ = "items"
    sku = Column(String(32), primary_key=True)
    quantity = Column(Integer)
    warehouse_id = Column(Integer, ForeignKey("warehouses.id"))
