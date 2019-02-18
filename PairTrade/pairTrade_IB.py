#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Time    : 2018/12/29 0029 10:17
# @Author  : Hadrianl 
# @File    : pairTrade_IB


from ib_insync import *
import logging
import uuid
from collections import OrderedDict, ChainMap
import datetime as dt
import asyncio

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger('CTPTrader')


class PairOrders:
    events = ('orderUpdateEvent', 'allFilledEvent',
              'forwardFilledEvent', 'guardFilledEvent',
              'forwardPartlyFilledEvent', 'guardPartlyFilledEvent',
              'finishedEvent')
    def __init__(self, pairInstrumentIDs, spread, buysell, vol, tolerant_timedelta):
        Event.init(self, PairOrders.events)
        self.id = uuid.uuid1()
        self.pairInstrumentIDs = pairInstrumentIDs
        self.spread = spread
        self.buysell = buysell
        self.vol = vol
        self.tolerant_timedelta = dt.timedelta(seconds=tolerant_timedelta)
        self.init_time = None
        self.trades = OrderedDict()
        self.extra_trades = OrderedDict()

        self._filled_queue = asyncio.Queue()

        self._order_log = []

        self._isFinished = False

        self._forwardFilled = False
        self._guardFilled = False
        self._allFilled = False

    def set_init_time(self):
        if self.init_time is None:
            self.init_time = dt.datetime.now()
            self.forwardFilledEvent = self.trades[0].filledEvent
            self.guardFilledEvent = self.trades[1].filledEvent

    async def handle_trade(self):
        while True:
            _ = await self._filled_queue.get()
            if all(trade.orderStatus == 'filled' for trade in self.trades):
                return True

    def netExposure(self):
        pos = 0
        neg = 0
        for ref, trade in ChainMap(self.trades, self.extra_trades).items():
            # if order is None:
            #     continue

            if trade.order.action == 'BUY':
                pos += trade.filled()
            else:
                neg += trade.filled()

        return pos - neg


    # def update_order(self, pOrder):
    #     if pOrder.OrderRef in self.orders:
    #         self.orders[pOrder.OrderRef] = pOrder  # 更新订单
    #         self._order_log.append(pOrder)
    #
    #         if pOrder.OrderStatus == b'0':
    #             if pOrder.OrderRef == list(self.orders.keys())[0]:
    #                 self._forwardFilled = True
    #                 self.forwardFilledEvent.emit()
    #             else:
    #                 self._guardFilled = True
    #                 self.guardFilledEvent.emit()
    #
    #             if self._forwardFilled & self._guardFilled:
    #                 self.allFilledEvent.emit()
    #                 self.finishedEvent.emit()
    #
    #             # if self._isFinished:
    #             #     self.finishedEvent.emit()
    #
    #         elif pOrder.OrderStatus == b'1':
    #             logger.warning(f'订单{pOrder}处于部分成交并在队列中，暂时未对改状态做全面严谨的处理')
    #             if pOrder.OrderRef == list(self.orders.keys())[0]:
    #                 self.forwardPartlyFilledEvent.emit()
    #             else:
    #                 self.guardPartlyFilledEvent.emit()

    @property
    def filled(self):
        return [t.orderStatus.status == 'Filled' for t in self.trades]

    @property
    def total(self):
        return [o.VolumeTotalOriginal for o in self.orders.values()]

    @property
    def remaining(self):
        return [o.VolumeTotal for o in self.orders.values()]

    async def isExpired(self):
        secs = (self.init_time + self.tolerant_timedelta - dt.datetime.now()).total_seconds()
        secs = max(secs, 0)
        await asyncio.sleep(secs)
        # return bool(self.init_time is not None and dt.datetime.now() > self.expireTime)
        return True

    def isAllFilled(self):
        return self._allFilled

    def isActive(self):
        return [bool(o in [b'3', b'1']) for o in self.orders.values()]

    def isFilled(self):
        return [self._forwardFilled, self._guardFilled]

    def isFinished(self):
        return self._isFinished

    @property
    def expireTime(self):
        return self.init_time + self.tolerant_timedelta

    def __repr__(self):
        return f'<PairOrder: {self.id}> instrument:{self.pairInstrumentIDs} spread:{self.spread} direction:{self.buysell}'

    def __iter__(self):
        return self.orders.items().__iter__()

class PairTrader(IB):
    def __init__(self, host, port, clientId=0, timeout=10):
        super(PairTrader, self).__init__()
        self.connect(host, port, clientId=clientId, timeout=timeout)
        # self.loopUntil()
        # self.waitUntil

        self._pairOrders_running = []  #List:
        self._pairOrders_finished = []
        self._lastUpdateTime = dt.datetime.now()
        # self.updateEvent += self._handle_expired_pairOrders

    async def placePairTrade(self, pairInstruments, spread, buysell, vol=1, tolerant_timedelta=30):
        assert buysell in ['BUY', 'SELL']
        ins1, ins2 = pairInstruments
        ticker1 = self.reqMktData(ins1)
        ticker2 = self.reqMktData(ins2)

        po = PairOrders(pairInstruments, spread, buysell, vol, tolerant_timedelta)
        po.tickers = [ticker1, ticker2]

        # 组合单预处理
        from operator import lt, gt
        comp = lt if buysell == 'BUY' else gt  # 小于价差买进组合，大于价差卖出组合
        if buysell == 'BUY':
            comp = lt
            ins1_direction = 'BUY'
            ins1_price = 'ask'
            ins2_direction = 'SELL'
            ins2_price = 'bid'
        else:
            comp = gt
            ins1_direction = 'SELL'
            ins1_price = 'bid'
            ins2_direction = 'BUY'
            ins2_price = 'ask'

        def po_finish():
            if not po._isFinished:
                po._isFinished = True
                self._pairOrders_finished.append(po)
                self._pairOrders_running.remove(po)

        po.finishedEvent += po_finish  # 主要用于配对交易完成的之后的处理，同running队列删除，移至finished队列。包括的情况有完全成交，单腿成交盈利平仓剩余撤单，全部撤单等情况


        def arbitrage(ticker):  # 套利下单判断
            price1 = getattr(ticker1, ins1_price)
            price2 = getattr(ticker2, ins2_price)
            current_spread = price1 - price2
            print(current_spread)
            # if comp(current_spread, spread):
            if True:
                ins1_lmt_order = LimitOrder(ins1_direction, vol, price1)
                ins2_lmt_order = LimitOrder(ins2_direction, vol, price2)
                trade1 = self.placeOrder(ticker1.contract, ins1_lmt_order)
                trade2 = self.placeOrder(ticker2.contract, ins2_lmt_order)
                keys = [self.wrapper.orderKey(o.clientId, o.orderId, o.permId) for o in [ins1_lmt_order, ins2_lmt_order]]
                for k, t in zip(keys, [trade1, trade2]):
                    po.trades[k] = t
                po.set_init_time()


                ticker1.updateEvent -= po
                ticker2.updateEvent -= po
                trade1.filledEvent += lambda fill: po._filled_queue.put_nowait(fill)
                trade2.filledEvent += lambda fill: po._filled_queue.put_nowait(fill)

        po.__call__ = arbitrage
        ticker1.updateEvent += po
        ticker2.updateEvent += po
        self._pairOrders_running.append(po)
        await self.unfilled_order_handle(po)

        return po

    def delPairTrade(self, pairOrders):
        for t in self.tickers():
            if pairOrders in t.updateEvent:
                t.updateEvent -= pairOrders

    async def unfilled_order_handle(self, pairOrder):  # 报单成交处理逻辑，***整个交易对保单之后的逻辑都在这里处理
        # now = dt.datetime.now()
        # if now - self._lastUpdateTime < dt.timedelta(seconds=1):
        #     return
        # else:
        #     self._lastUpdateTime = now
        try:
            await asyncio.wait_for(pairOrder.handle_trade(), pairOrder.tolerant_timedelta.total_seconds())
        except asyncio.TimeoutError:
            logger.info(f'<unfilled_order_handle>{pairOrder}已过期')
            try:
                while pairOrder in self._pairOrders_running:
                    await self._handle_expired_pairOrders(pairOrder)  # FIXME:可以深入优化
            except Exception as e:
                logger.exception(f'<unfilled_order_handle>处理过期配对报单错误')


        # async for po in self._pairOrders_running:
        #
        #     if po.isExpired():
        #         logger.info(f'<unfilled_order_handle>{po}已过期')
        #         try:
        #             self._handle_expired_pairOrders(po)  # FIXME:可以深入优化
        #         except Exception as e:
        #             logger.exception(f'<unfilled_order_handle>处理过期配对报单错误')


    async def _handle_expired_pairOrders(self, po):
        net = po.netExposure()
        pnl = self._calc_pnl(po)
        if pnl >0:
            for key, trade in ChainMap(po.trades, po.extra_trades).items():
                if trade.orderStatus.status in OrderStatus.ActiveStates:  # 把队列中的报单删除
                    self._close_after_del(trade.order)
            else:
                po.finishedEvent.emit()
                return

        if net == 0:
            # logger.info(
            #     f'<_handle_expired_pairOrders>pairOrders:{pairOrders.id} 净暴露头寸：{net} 已盈利点数->{pnl}， 撤销未完全成交报单')
            for key, trade in ChainMap(po.trades, po.extra_trades).items():
                if trade.orderStatus.status in OrderStatus.ActiveStates:  # 把队列中的报单删除
                    self.cancelOrder(trade.order)
            else:
                po.finishedEvent.emit()

        elif net > 0:
            # logger.info(
            #     f'<_handle_expired_pairOrders>pairOrders:{pairOrders.id} 净暴露头寸：{net} 理论盈利点数->{pnl}， 撤销所有报单，并平掉暴露仓位')
            for key, trade in ChainMap(po.trades, po.extra_trades).items():
                if trade.orderStatus.status in OrderStatus.ActiveStates:  # 把队列中的报单删除
                    self._modify_to_op_price(trade, net)

        elif net < 0:
            # logger.info(
            #     f'<_handle_expired_pairOrders>pairOrders:{pairOrders.id} 净暴露头寸：{net} 理论盈利点数->{pnl}， 撤销所有报单，并平掉暴露仓位')
            for key, trade in ChainMap(po.trades, po.extra_trades).items():
                if trade.orderStatus.status in OrderStatus.ActiveStates:  # 把队列中的报单删除
                    self._modify_to_op_price(trade, net)

        await asyncio.sleep(1)


    def _modify_to_op_price(self, trade, net):
        def insert_after_cancel(t): # 收到订单取消时间后，马上报新单
            if net < 0:
                action = 'BUY'
                price = getattr(self.wrapper.tickers[id(t.contract)], 'ask')
            elif net > 0:
                action = 'SELL'
                price = getattr(self.wrapper.tickers[id(t.contract)], 'bid')

            lmt_order = LimitOrder(action, abs(net), price)
            new_trade = self.placeOrder(t.contract, lmt_order)

            for po in self._pairOrders_running:
                if t in ChainMap(po.trades, po.extra_trades).values():
                    po.extra_trades[
                        self.wrapper.orderKey(t.order.clientId, t.order.orderId, t.order.permId)] = new_trade
                    break

        trade.cancelledEvent += insert_after_cancel
        self.cancelOrder(trade.order)

    def _close_after_del(self, trade):
        def insert_after_cancel(t): # 收到订单取消时间后，马上平仓
            if t.order.action == 'SELL':
                action = 'BUY'
                price = getattr(self.wrapper.tickers[id(t.contract)], 'ask')
            else:
                action = 'SELL'
                price = getattr(self.wrapper.tickers[id(t.contract)], 'bid')


            lmt_order = LimitOrder(action, t.filled(), price)
            new_trade = self.placeOrder(t.contract, lmt_order)

            for po in self._pairOrders_running:
                if t in ChainMap(po.trades, po.extra_trades).values():
                    po.extra_trades[self.wrapper.orderKey(t.order.clientId, t.order.orderId, t.order.permId)] = new_trade
                    break


        trade.cancelledEvent += insert_after_cancel
        self.cancelOrder(trade.order)

    def _calc_pnl(self, pairOrders):
        total_pnl = 0
        for key, t in ChainMap(pairOrders.trades, pairOrders.extra_trades).items():
            # if t is None:
            #     continue
            ticker = self.wrapper.tickers[id(t.contract)]
            if t.order.action == 'BUY':
                pnl = (ticker.bid - t.order.lmtPrice) * t.filled() * t.contract.multiplier
            else:
                pnl = (ticker.ask - t.order.lmtPrice) * t.filled() * t.contract.multiplier

            total_pnl += pnl

        return total_pnl



if __name__ == '__main__':
    ib = IB()
    ib.connect('127.0.0.1', 7497, clientId=0, timeout=10)

