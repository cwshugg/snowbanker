# This is my "threshold" strategy. It watches a list of N different stocks and
# chooses to buy/sell depending on when a stock crosses a certain threshold
# (where a threshold is a specific percent increase/decrease from its previous
# buy/sell price).
#
#   Connor Shugg

# Imports
import os
import sys
from enum import Enum
import json
from datetime import datetime

# Enable import from the parent directory
strat_dpath = os.path.dirname(os.path.realpath(__file__))
src_dpath = os.path.dirname(strat_dpath)
if src_dpath not in sys.path:
    sys.path.append(src_dpath)

# My imports
from sbi.strat import Strategy
from sbi.asset import PriceDataPoint, Asset, AssetGroup
import sbi.utils as utils
from sbi.utils import IR, float_to_str_dollar, float_to_str_maybe_round
from sbi.api import TradeOrder, TradeOrderAction

# Strategy globals
base_buy = 20.0     # base dollar amount to buy for a new asset
thresh_buy = 0.01   # percentage the asset must drop before buying
thresh_sell = 0.01  # percentage the asset must rise before selling
order_cooldown = 43200 # amount of seconds to wait between orders
symbols = []        # list of symbol names (assets o manage)


# ============================ Helper Functions ============================= #
# Helper function used to generate a file name for a specific asset for
# this strategy
def symbol_to_asset_fname(name: str) -> str:
    return utils.str_to_fname("asset_%s" % name.lower(), extension="json")


# ========================== Asset Data Structures ========================== #
# Enum used to track specific "modes" assets can be in. The modes are:
#   WATCH MODE      The strategy is waiting for the right time to buy
#   HOLD MODE       The strategy is waiting for the right time to sell
class AssetMode(Enum):
    UNKNOWN = -1
    WATCH = 0
    HOLD = 1

# A wrapper for an asset that contains a little extra data needed for this
# strategy.
class AssetData():
    # Constructor
    def __init__(self, asset: Asset):
        self.asset: Asset = asset
        self.mode: AssetMode = AssetMode.UNKNOWN
        self.thistory = [] # list of PDPs of previous transactions

    # ------------------------- Transaction History ------------------------- #
    # Appends the given price data point to the asset data's transaction
    # history.
    def thistory_append(self, pdp: PriceDataPoint) -> bool:
        self.thistory.append(pdp)
        return True

    # Returns the most recent price data point, or None if there aren't any.
    def thistory_latest(self) -> PriceDataPoint:
        thlen = len(self.thistory)
        if thlen == 0:
            return None
        return self.thistory[thlen - 1]

    # --------------------------- JSON Functions ---------------------------- #
    # Converts the object to JSON and returns it.
    def json_make(self) -> dict:
        # first build the asset's JSON
        jdata = self.asset.json_make()
        # add in the current mode
        jdata["mode"] = self.mode.value
        # add the transaction history
        pdps = []
        for pdp in self.thistory:
            pdps.append(pdp.json_make())
        jdata["thistory"] = pdps
        return jdata

    # Attempts to parse a JSON object and return an AssetData object.
    # Returns None on failure to parse anything.
    @staticmethod
    def json_parse(jdata: dict):
        # first attempt to load the asset
        a = Asset.json_parse(jdata)
        if a == None:
            return None
        ad = AssetData(a)
        
        # check for other expected keys
        expected = [["mode", int], ["thistory", list]]
        if not utils.json_check_keys(jdata, expected):
            return None
        # load the mode and transaction history
        ad.mode = AssetMode(jdata["mode"])
        for pdp in jdata["thistory"]:
            if pdp == None:
                continue
            # parse the JSON and return on failure to parse
            pdp_obj = PriceDataPoint.json_parse(pdp)
            if pdp_obj == None:
                return None
            ad.thistory_append(pdp_obj)
        return ad

    # --------------------------- File Functions ---------------------------- #
    # Attempts to write itself out to disk.
    def save(self, dpath: str) -> IR:
        jdata = self.json_make()
        # create the expected file path
        fname = symbol_to_asset_fname(self.asset.symbol)
        fpath = os.path.join(dpath, fname)
        # write to the file
        jstr = json.dumps(jdata, indent=4)
        return utils.file_write_all(fpath, jstr)
    
    # Attempts to load an AssetData in from disk.
    @staticmethod
    def load(symbol: str, dpath: str) -> IR:
        fname = symbol_to_asset_fname(symbol)
        fpath = os.path.join(dpath, fname)
        # attempt to load the file
        res = utils.file_read_all(fpath)
        if not res.success:
            return res
        # attempt to parse as JSON
        jdata = utils.json_try_loads(res.data)
        if jdata == None:
            return IR(False, msg="failed to parse JSON data from file: %s" % fpath)
        # attempt to parse as an AssetData
        ad = AssetData.json_parse(jdata)
        if ad == None:
            return IR(False, msg="failed to parse AssetData from file: %s" % fpath)
        return IR(True, data=ad)


# ============================= Strategy Class ============================== #
# Main strategy class.
class TStrat(Strategy):
    assets_fname = "assets.json"

    # Overridden init function.
    def init(self, dpath: str, config_fpath=None) -> IR:
        # run the inherited init sequence first
        res = super().init(dpath)
        if not res.success:
            return res
        
        # if a config path was given, load it
        if config_fpath != None:
            res = self.config_load(config_fpath)
            if not res.success:
                return res
        # save the symbols
        global symbols
        symbols = res.data
            
        return IR(True)

    # Main strategy tick function.
    def tick(self) -> IR:
        # check if the markets are open or not
        res = self.api.get_market_status()
        if not res.success:
            return res
        if not res.data:
            self.log("markets are closed. Skipping this tick.")

        # first, retrieve all assets
        res = self.retrieve_assets()
        if not res.success:
            return res
        adata = res.data

        # iterate through each asset data object
        vsum = 0.0 # sum of all assets' current value
        for ad in adata:
            own_shares = ad.asset.quantity > 0.0
            
            # ----------------------- Value Retrieval ----------------------- #
            # compute the maximum and minimum PDPs from the asset's history to
            # help us decide what to do. If not enough data is collected yet,
            # wait for the next tick
            amin = ad.asset.phistory_min()
            amax = ad.asset.phistory_max()
            acurr = ad.asset.phistory_latest()
            no_history = amin == None or amax == None or acurr == None
            if no_history:
                self.log("%s has no recorded history. " % ad.asset.symbol)
            vsum += acurr.value() * ad.asset.quantity
            
            # ----------------------- Order Cooldown ------------------------ #
            # if we've already placed an order within the cooldown time, move on
            global order_cooldown
            now_secs = datetime.now().timestamp()
            ltran = ad.thistory_latest() # latest transaction
            if ltran != None:
                ltran_secs = ltran.timestamp_total_seconds()
                diff_secs = now_secs - ltran_secs
                # if the time diff is less than the cooldown, we can't place
                # another order for this tick
                if diff_secs < order_cooldown:
                    #self.log("%s An order was made too recently "
                    #         "(%d seconds ago). Skipping." %
                    #         (utils.STAB_TREE1, diff_secs))
                    continue
            
            # ------------------------ Fancy Logging ------------------------ #
            # also, compute how far away the current price is from both the
            # recorded minimum and recorded maximum (by computing a percent
            # out of the range between MAX and MIN)
            percent_to_max = 0.0
            if not no_history:
                if amax.value() != amin.value():
                    percent_to_max = ((acurr.value() - amin.value()) /
                                        (amax.value() - amin.value()))

                # log some information about the asset's current stats
                self.log("%s: %f shares * %s = %s" % (ad.asset.symbol, ad.asset.quantity,
                        utils.float_to_str_dollar(acurr.price),
                        utils.float_to_str_dollar(acurr.value() * ad.asset.quantity)))
                progbar = "Current Price [%-10s|" % utils.float_to_str_dollar(amin.value())
                progbar_len = 25
                for i in range(int(progbar_len * percent_to_max)):
                    progbar += "*"
                for i in range(progbar_len - int(progbar_len * percent_to_max)):
                    progbar += " "
                progbar += "|%10s]" % utils.float_to_str_dollar(amax.value())
                self.log("%s%s" % (utils.STAB_TREE2, progbar))
            
            # ------------------- Actual Strategic Stuff -------------------- #

            # if we presently down own any shares, we'll buy some
            if not own_shares:
                # if there's no recorded history OR our asset is marked as
                # having a quantity of ZERO, we'll buy a minimum value of $1.00
                # to put the stock "on the board" so we can track it with
                # the Alpaca API in future ticks
                if no_history or ad.asset.quantity == 0.0:
                    self.log("%sBuying minimum amount." % utils.STAB_TREE2)
                    order = TradeOrder(ad.asset.symbol, TradeOrderAction.BUY, 1.00)
                    order_result: TradeOrder = self.place_order(ad, order)
                continue
            
            # if the current value is below the lower threshold, we'll buy some
            # amount of the stock
            thresh_buy_percent = 0.5 - thresh_buy
            if percent_to_max <= thresh_buy_percent:
                # we'll purchase an amount based on how close the price is from
                # the threshold value
                buy_amount = (1.0 - (percent_to_max / thresh_buy_percent)) * base_buy
                buy_amount = max(1.0, buy_amount)

                # place the order
                self.log("%sPrice is below BUY threshold. Placing order for BUY %s." %
                         (utils.STAB_TREE2, utils.float_to_str_dollar(buy_amount)))
                order = TradeOrder(ad.asset.symbol, TradeOrderAction.BUY, buy_amount)
                order_result: TradeOrder = self.place_order(ad, order)
                continue

            # if the current value is above the upper threshold, we'll sell some
            # amount of the stock
            thresh_sell_percent = 0.5 + thresh_sell
            if percent_to_max >= thresh_sell_percent:
                # we'll sell an amount based on how close the price is from the
                # threshold value. We also want to make sure we don't try to
                # sell more than we own, and we don't want to sell ALL of it
                multiplier = (percent_to_max - thresh_sell_percent) / (1.0 - thresh_sell_percent)
                sell_amount = multiplier * base_buy
                sell_amount = min(acurr.value() * ad.asset.quantity, sell_amount)
                sell_amount = max(0.0, round(sell_amount - 1.0, 2))
                if sell_amount == 0.0:
                    self.log("%sNot enough to sell. Holding." % utils.STAB_TREE1)
                    continue

                # place the order
                self.log("%sPrice is below SELL threshold. Placing order for SELL %s." %
                         (utils.STAB_TREE2, utils.float_to_str_dollar(sell_amount)))
                order = TradeOrder(ad.asset.symbol, TradeOrderAction.SELL, sell_amount)
                order_result: TradeOrder = self.place_order(ad, order)
                continue

            # if all else fails, we'll hold
            self.log("%sPrice outside of thresholds. Holding." % utils.STAB_TREE1)
            continue
        
        self.log("Current asset value sum: %s" % utils.float_to_str_dollar(vsum))
        return IR(True)
    
    # Helper function for placing an order. Logs messages and returns the order
    # struct returned by the API call.
    def place_order(self, ad: AssetData, order: TradeOrder) -> TradeOrder:
        # send the order and log accordingly
        res = self.api.send_order(order)
        if not res.success:
            self.log("%sorder failed: %s" % (utils.STAB_TREE1, res.message))
            return None
        # log a success message and return the order result
        order_result = res.data
        self.log("%sorder succeeded: [value: %s] [id: %s]" %
                (utils.STAB_TREE1, order_result.value, order_result.id))
        
        # save the order details to the asset data's history, then write it out
        # to disk
        current_price = order_result.value / order_result.quantity
        pdp = PriceDataPoint(current_price, datetime.now(),
                             quantity=order_result.quantity)
        ad.thistory_append(pdp)
        ad.save(self.work_dpath)
        return order_result


    # Function used to retrieve the latest asset information, either stored on
    # disk or retrieved from the Alpaca web API.
    def retrieve_assets(self) -> IR:
        # first, load all assets from our account
        res = self.api.get_assets()
        if not res.success:
            return res
        assets: AssetGroup = res.data

        # iterate through the retrieved assets and remove any that this
        # strategy isn't tracking
        global symbols
        for a in assets:
            if a.symbol not in symbols:
                assets.remove(a.symbol)

        # take the global symbol list and search for the correct file for each
        adata = [] # array of AssetData objects
        for sym in symbols:
            # attempt to load the asset data from disk
            res = AssetData.load(sym, self.work_dpath)
            ad = None
            if res.success:
                ad = res.data
            
            # search the retrieved assets for the correct symbol, and make one
            # if we couldn't find one
            a = assets.search(sym)
            if a == None:
                a = Asset(sym, sym, 0.0)
            else:
                if ad != None:
                    ad.asset.phistory_append(a.phistory_latest())
                    ad.asset.quantity = a.quantity
            # if we didn't load an asset data, make it
            if ad == None:
                ad = AssetData(a)
            
            # append to the array and write the asset data back out to disk
            adata.append(ad)
            ad.save(self.work_dpath)
        
        return IR(True, data=adata)
    
    # Used to load in the strategy config file. Returns a list of string symbol
    # names the strategy must work with.
    def config_load(self, fpath: str) -> IR:
        # make sure the path is a file
        if not os.path.isfile(fpath):
            return IR(False, msg="the given file path (%s) is not a file" % fpath)
        
        # attempt to read and parse the JSON data from the file
        res = utils.file_read_all(fpath)
        if not res.success:
            return res
        
        # attempt to parse the data as JSON
        jdata = utils.json_try_loads(res.data)
        if jdata == None:
            return IR(False, msg="failed to load JSON data from: %s" % fpath)
        
        # check the expected keys
        expected = [["base_buy", float],
                    ["thresh_buy", float], ["thresh_sell", float],
                    ["order_cooldown", int], ["symbols", list]]
        if not utils.json_check_keys(jdata, expected):
            return IR(False, msg="JSON data from file (%s) is missing keys" % fpath)
        
        # assign fields
        global base_buy, thresh_buy, thresh_sell, order_cooldown
        base_buy = jdata["base_buy"]
        thresh_buy = jdata["thresh_buy"]
        thresh_sell = jdata["thresh_sell"]
        order_cooldown = jdata["order_cooldown"]

        # make sure the symbols aren't empty
        syms = jdata["symbols"]
        if len(syms) == 0:
            return IR(False, msg="the given config file (%s) contains zero symbols" % fpath)
        return IR(True, data=syms)
