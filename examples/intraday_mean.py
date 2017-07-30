import pandas as pd
import numpy as np
import h5py
import time
import hpat

# adopted from:
# http://www.pythonforfinance.net/2017/02/20/intraday-stock-mean-reversion-trading-backtest-in-python/

@hpat.jit(locals={'s_open': hpat.float64[:], 's_high': hpat.float64[:],
        's_low': hpat.float64[:], 's_close': hpat.float64[:],
        's_vol': hpat.float64[:]})
def intraday_mean_revert():
    file_name = "stock_data.hdf5"
    f = h5py.File(file_name, "r")
    sym_list = list(f.values()) #['IBM']
    nsyms = len(sym_list)

    t1 = time.time()
    for i in hpat.prange(nsyms):
        symbol = sym_list[i]

        s_open = f[symbol+'/Open'][:]
        s_high = f[symbol+'/High'][:]
        s_low = f[symbol+'/Low'][:]
        s_close = f[symbol+'/Close'][:]
        s_vol = f[symbol+'/Volume'][:]
        df = pd.DataFrame({'Open': s_open, 'High': s_high, 'Low': s_low,
                            'Close': s_close, 'Volume': s_vol,})

        #create column to hold our 90 day rolling standard deviation
        df['Stdev'] = df['Close'].rolling(window=90).std()
        #print(np.array(df['Stdev'])[-1])

        #create a column to hold our 20 day moving average
        df['Moving Average'] = df['Close'].rolling(window=20).mean()

        #create a column which holds a TRUE value if the gap down from previous day's low to next
        #day's open is larger than the 90 day rolling standard deviation
        df['Criteria1'] = (df['Open'] - df['Low'].shift(1)) < -df['Stdev']

        #create a column which holds a TRUE value if the opening price of the stock is above the 20 day moving average
        df['Criteria2'] = df['Open'] > df['Moving Average']

        #create a column that holds a TRUE value if both above criteria are also TRUE
        df['BUY'] = df['Criteria1'] & df['Criteria2']

        #calculate daily % return series for stock
        df['Pct Change'] = (df['Close'] - df['Open']) / df['Open']

        #create a strategy return series by using the daily stock returns where the trade criteria above are met
        df['Rets'] = df['Pct Change'][df['BUY'] == True]
        print(df['Rets'].sum())
    print("time:", time.time()-t1)

intraday_mean_revert()