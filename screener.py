import yfinance as yf
import pandas as pd
import time
import os
import requests
# -----------------------------
# 使用者設定lll
# -----------------------------
shadow_ratio = 1.5  # 上影線至少是實體的 1.5 倍
upper_shadow_min_pct = 0.02  # 前一天上影線至少佔收盤價的 2%
taiwan_stocks = ["1101.TW","1102.TW","1103.TW","1104.TW","1108.TW","1109.TW","1110.TW","1201.TW","1203.TW","1210.TW","1213.TW","1215.TW","1216.TW","1217.TW"
 ,"1218.TW","1219.TW","1220.TW","1225.TW","1227.TW","1229.TW","1231.TW","1232.TW","1233.TW","1234.TW","1235.TW","1236.TW"
 ,"1256.TW","1301.TW","1303.TW","1304.TW","1305.TW","1307.TW","1308.TW","1309.TW","1310.TW","1312.TW","1313.TW","1314.TW"
 ,"1315.TW","1316.TW","1319.TW","1321.TW","1323.TW","1324.TW","1325.TW","1326.TW","1337.TW","1338.TW","1339.TW","1340.TW",
 "1341.TW","1342.TW","1402.TW","1409.TW","1410.TW","1413.TW","1414.TW","1416.TW","1417.TW","1418.TW","1419.TW","1423.TW",
 "1432.TW","1434.TW","1435.TW","1436.TW","1437.TW","1438.TW","1439.TW","1440.TW","1441.TW","1442.TW","1443.TW","1444.TW",
 "1445.TW","1446.TW","1447.TW","1449.TW","1451.TW","1452.TW","1453.TW","1454.TW","1455.TW","1456.TW","1457.TW","1459.TW","1460.TW","1463.TW","1464.TW","1465.TW","1466.TW","1467.TW","1468.TW","1470.TW","1471.TW","1472.TW","1473.TW","1474.TW","1475.TW","1476.TW","1477.TW","1503.TW","1504.TW","1506.TW","1512.TW","1513.TW","1514.TW","1515.TW","1516.TW","1517.TW","1519.TW","1521.TW","1522.TW","1524.TW","1525.TW","1526.TW","1527.TW","1528.TW","1529.TW","1530.TW","1531.TW","1532.TW","1533.TW","1535.TW","1536.TW","1537.TW","1538.TW","1539.TW","1540.TW","1541.TW","1558.TW","1560.TW","1563.TW","1568.TW","1582.TW","1583.TW","1587.TW","1589.TW","1590.TW","1597.TW","1598.TW","1603.TW","1604.TW","1605.TW","1608.TW","1609.TW","1611.TW","1612.TW","1614.TW","1615.TW","1616.TW","1617.TW","1618.TW","1626.TW","1702.TW","1707.TW","1708.TW","1709.TW","1710.TW","1711.TW","1712.TW","1713.TW","1714.TW","1717.TW","1718.TW","1720.TW","1721.TW","1722.TW","1723.TW","1725.TW","1726.TW","1727.TW","1730.TW","1731.TW","1732.TW","1733.TW","1734.TW","1735.TW","1736.TW","1737.TW","1752.TW","1760.TW","1762.TW","1773.TW","1776.TW","1783.TW","1786.TW","1789.TW","1795.TW","1802.TW","1805.TW","1806.TW","1808.TW","1809.TW","1810.TW","1817.TW","1903.TW","1904.TW","1905.TW","1906.TW","1907.TW","1909.TW","2002.TW","2006.TW","2007.TW","2008.TW","2009.TW","2010.TW","2012.TW","2013.TW","2014.TW","2015.TW","2017.TW","2020.TW","2022.TW","2023.TW","2024.TW","2025.TW","2027.TW","2028.TW","2029.TW","2030.TW","2031.TW","2032.TW","2033.TW","2034.TW","2038.TW","2049.TW","2059.TW","2062.TW","2069.TW","2101.TW","2102.TW","2103.TW","2104.TW","2105.TW","2106.TW","2107.TW","2108.TW","2109.TW","2114.TW","2115.TW","2201.TW","2204.TW","2206.TW","2207.TW","2208.TW","2211.TW","2227.TW","2228.TW","2231.TW","2233.TW","2236.TW","2239.TW","2241.TW","2243.TW","2247.TW","2248.TW","2250.TW","2301.TW","2302.TW","2303.TW","2305.TW","2308.TW","2312.TW","2313.TW","2314.TW","2316.TW","2317.TW","2321.TW","2323.TW","2324.TW","2327.TW","2328.TW","2329.TW","2330.TW","2331.TW","2332.TW","2337.TW","2338.TW","2340.TW","2342.TW","2344.TW","2345.TW","2347.TW","2348.TW","2349.TW","2351.TW","2352.TW","2353.TW","2354.TW","2355.TW","2356.TW","2357.TW","2359.TW","2360.TW","2362.TW","2363.TW","2364.TW","2365.TW","2367.TW","2368.TW","2369.TW","2371.TW","2373.TW","2374.TW","2375.TW","2376.TW","2377.TW","2379.TW","2380.TW","2382.TW","2383.TW","2385.TW","2387.TW","2388.TW","2390.TW","2392.TW","2393.TW","2395.TW","2397.TW","2399.TW","2401.TW","2402.TW","2404.TW","2405.TW","2406.TW","2408.TW","2409.TW","2412.TW","2413.TW","2414.TW","2415.TW","2417.TW","2419.TW","2420.TW","2421.TW","2423.TW","2424.TW","2425.TW","2426.TW","2427.TW","2428.TW","2429.TW","2430.TW","2431.TW","2433.TW","2434.TW","2436.TW","2438.TW","2439.TW","2440.TW","2441.TW","2442.TW","2444.TW","2449.TW","2450.TW","2451.TW","2453.TW","2454.TW","2455.TW","2457.TW","2458.TW","2459.TW","2460.TW","2461.TW","2462.TW","2464.TW","2465.TW","2466.TW","2467.TW","2468.TW","2471.TW","2472.TW","2474.TW","2476.TW","2477.TW","2478.TW","2480.TW","2481.TW","2482.TW","2483.TW","2484.TW","2485.TW","2486.TW","2488.TW","2489.TW","2491.TW","2492.TW","2493.TW","2495.TW","2496.TW","2497.TW","2498.TW","2501.TW","2504.TW","2505.TW","2506.TW","2509.TW","2511.TW","2514.TW","2515.TW","2516.TW","2520.TW","2524.TW","2527.TW","2528.TW","2530.TW","2534.TW","2535.TW","2536.TW","2537.TW","2538.TW","2539.TW","2540.TW","2542.TW","2543.TW","2545.TW","2546.TW","2547.TW","2548.TW","2597.TW","2601.TW","2603.TW","2605.TW","2606.TW","2607.TW","2608.TW","2609.TW","2610.TW","2611.TW","2612.TW","2613.TW","2614.TW","2615.TW","2616.TW","2617.TW","2618.TW","2630.TW","2633.TW","2634.TW","2636.TW","2637.TW","2642.TW","2645.TW","2646.TW","2701.TW","2702.TW","2704.TW","2705.TW","2706.TW","2707.TW","2712.TW","2722.TW","2723.TW","2727.TW","2731.TW","2739.TW","2748.TW","2753.TW","2762.TW","2801.TW","2809.TW","2812.TW","2816.TW","2820.TW","2832.TW","2834.TW","2836.TW","2838.TW","2845.TW","2849.TW","2850.TW","2851.TW","2852.TW","2855.TW","2867.TW","2880.TW","2881.TW","2882.TW","2883.TW","2884.TW","2885.TW","2886.TW","2887.TW","2889.TW","2890.TW","2891.TW","2892.TW","2897.TW","2901.TW","2903.TW","2904.TW","2905.TW","2906.TW","2908.TW","2910.TW","2911.TW","2912.TW","2913.TW","2915.TW","2923.TW","2929.TW","2939.TW","2945.TW","3002.TW","3003.TW","3004.TW","3005.TW","3006.TW","3008.TW","3010.TW","3011.TW","3013.TW","3014.TW","3015.TW","3016.TW","3017.TW","3018.TW","3019.TW","3021.TW","3022.TW","3023.TW","3024.TW","3025.TW","3026.TW","3027.TW","3028.TW","3029.TW","3030.TW","3031.TW","3032.TW","3033.TW","3034.TW","3035.TW","3036.TW","3037.TW","3038.TW","3040.TW","3041.TW","3042.TW","3043.TW","3044.TW","3045.TW","3046.TW","3047.TW","3048.TW","3049.TW","3050.TW","3051.TW","3052.TW","3054.TW","3055.TW","3056.TW","3057.TW","3058.TW","3059.TW","3060.TW","3062.TW","3090.TW","3092.TW","3094.TW","3130.TW","3135.TW","3138.TW","3149.TW","3164.TW","3167.TW","3168.TW","3189.TW","3209.TW","3229.TW","3231.TW","3257.TW","3266.TW","3296.TW","3305.TW","3308.TW","3311.TW","3312.TW","3321.TW","3338.TW","3346.TW","3356.TW","3376.TW","3380.TW","3406.TW","3413.TW","3416.TW","3419.TW","3432.TW","3437.TW","3443.TW","3447.TW","3450.TW","3454.TW","3481.TW","3494.TW","3501.TW","3504.TW","3515.TW","3518.TW","3528.TW","3530.TW","3532.TW","3533.TW","3535.TW","3543.TW","3545.TW","3550.TW","3557.TW","3563.TW","3576.TW","3583.TW","3588.TW","3591.TW","3592.TW","3593.TW","3596.TW","3605.TW","3607.TW","3617.TW","3622.TW","3645.TW","3652.TW","3653.TW","3661.TW","3665.TW","3669.TW","3673.TW","3679.TW","3686.TW","3694.TW","3701.TW","3702.TW","3703.TW","3704.TW","3705.TW","3706.TW","3708.TW","3711.TW","3712.TW","3714.TW","3715.TW","3716.TW","3717.TW","4104.TW","4106.TW","4108.TW","4119.TW","4133.TW","4137.TW","4142.TW","4148.TW","4155.TW","4164.TW","4190.TW","4306.TW","4414.TW","4426.TW","4438.TW","4439.TW","4440.TW","4441.TW","4526.TW","4532.TW","4536.TW","4540.TW","4545.TW","4551.TW","4552.TW","4555.TW","4557.TW","4560.TW","4562.TW","4564.TW","4566.TW","4569.TW","4571.TW","4572.TW","4576.TW","4581.TW","4583.TW","4588.TW","4720.TW","4722.TW","4736.TW","4737.TW","4739.TW","4746.TW","4755.TW","4763.TW","4764.TW","4766.TW","4770.TW","4771.TW","4807.TW","4904.TW","4906.TW","4912.TW","4915.TW","4916.TW","4919.TW","4927.TW","4930.TW","4934.TW","4935.TW","4938.TW","4942.TW","4943.TW","4949.TW","4952.TW","4956.TW","4958.TW","4960.TW","4961.TW","4967.TW","4968.TW","4976.TW","4977.TW","4989.TW","4994.TW","4999.TW","5007.TW","5203.TW","5215.TW","5222.TW","5225.TW","5234.TW","5243.TW","5244.TW","5258.TW","5269.TW","5283.TW","5284.TW","5285.TW","5288.TW","5292.TW","5306.TW","5388.TW","5434.TW","5469.TW","5471.TW","5484.TW","5515.TW","5519.TW","5521.TW","5522.TW","5525.TW","5531.TW","5533.TW","5534.TW","5538.TW","5546.TW","5607.TW","5608.TW","5706.TW","5871.TW","5876.TW","5880.TW","5906.TW","5907.TW","6005.TW","6024.TW","6108.TW","6112.TW","6115.TW","6116.TW","6117.TW","6120.TW","6128.TW","6133.TW","6136.TW","6139.TW","6141.TW","6142.TW","6152.TW","6153.TW","6155.TW","6164.TW","6165.TW","6166.TW","6168.TW","6176.TW","6177.TW","6183.TW","6184.TW","6189.TW","6191.TW","6192.TW","6196.TW","6197.TW","6201.TW","6202.TW","6205.TW","6206.TW","6209.TW","6213.TW","6214.TW","6215.TW","6216.TW","6224.TW","6225.TW","6226.TW","6230.TW","6235.TW","6239.TW","6243.TW","6257.TW","6269.TW","6271.TW","6277.TW","6278.TW","6281.TW","6282.TW","6283.TW","6285.TW","6405.TW","6409.TW","6412.TW","6414.TW","6415.TW","6416.TW","6426.TW","6431.TW","6438.TW","6442.TW","6443.TW","6446.TW","6449.TW","6451.TW","6456.TW","6464.TW","6472.TW","6477.TW","6491.TW","6504.TW","6505.TW","6515.TW","6525.TW","6526.TW","6531.TW","6533.TW","6541.TW","6550.TW","6552.TW","6558.TW","6573.TW","6579.TW","6581.TW","6582.TW","6585.TW","6589.TW","6591.TW","6592.TW","6598.TW","6605.TW","6606.TW","6625.TW","6641.TW","6655.TW","6657.TW","6658.TW","6666.TW","6668.TW","6669.TW","6670.TW","6671.TW","6672.TW","6674.TW","6689.TW","6691.TW","6695.TW","6698.TW","6706.TW","6715.TW","6719.TW","6742.TW","6743.TW","6753.TW","6754.TW","6756.TW","6757.TW","6768.TW","6770.TW","6776.TW","6781.TW","6782.TW","6789.TW","6790.TW","6792.TW","6796.TW","6799.TW","6805.TW","6806.TW","6807.TW","6830.TW","6834.TW","6835.TW","6838.TW","6861.TW","6862.TW","6863.TW","6869.TW","6873.TW","6885.TW","6887.TW","6890.TW","6901.TW","6902.TW","6906.TW","6909.TW","6914.TW","6916.TW","6918.TW","6919.TW","6923.TW","6928.TW","6931.TW","6933.TW","6936.TW","6937.TW","6944.TW","6952.TW","6957.TW","6958.TW","6962.TW","6965.TW","6994.TW","7705.TW","7721.TW","7722.TW","7732.TW","7736.TW","7749.TW","7765.TW","7780.TW","7799.TW","8011.TW","8016.TW","8021.TW","8028.TW","8033.TW","8039.TW","8045.TW","8046.TW","8070.TW","8072.TW","8081.TW","8101.TW","8103.TW","8104.TW","8105.TW","8110.TW","8112.TW","8114.TW","8131.TW","8150.TW","8163.TW","8201.TW","8210.TW","8213.TW","8215.TW","8222.TW","8249.TW","8261.TW","8271.TW","8341.TW","8367.TW","8374.TW","8404.TW","8411.TW","8422.TW","8429.TW","8438.TW","8442.TW","8443.TW","8454.TW","8462.TW","8463.TW","8464.TW","8466.TW","8467.TW","8473.TW","8476.TW","8478.TW","8481.TW","8482.TW","8488.TW","8499.TW","8926.TW","8940.TW","8996.TW","9802.TW","9902.TW","9904.TW","9905.TW","9906.TW","9907.TW","9908.TW","9910.TW","9911.TW","9912.TW","9914.TW","9917.TW","9918.TW","9919.TW","9921.TW","9924.TW","9925.TW","9926.TW","9927.TW","9928.TW","9929.TW","9930.TW","9931.TW","9933.TW","9934.TW","9935.TW","9937.TW","9938.TW","9939.TW","9940.TW","9941.TW","9942.TW","9943.TW","9944.TW","9945.TW","9946.TW","9955.TW","9958.TW"]
# S&P 500 股票列表（可用 yfinance Ticker Symbols 或自建 CSV）
# 這裡用簡單示例，可替換成完整列表
sp500_tickers = [
    # 科技巨頭
    "AAPL", "MSFT", "AMZN", "GOOGL", "GOOG", "TSLA", "NVDA", "META", "AVGO", "NFLX", "ADBE", "CRM", "ORCL", "INTC", "AMD", "QCOM", "CSCO", "IBM", "NOW", "SNPS", "CDNS", "ANET", "PANW", "CRWD", "PLTR", "COIN", "DASH", "UBER", "ABNB", "SHOP", "TEAM", "MDB", "MELI", "PDD", "ARM", "ASML", "GFS", "APP", "TTD", "DDOG", "ZS", "MSTR",
    
    # 金融股
    "JPM", "BAC", "WFC", "GS", "MS", "C", "AXP", "V", "MA", "COF", "SCHW", "BLK", "SPGI", "MCO", "ICE", "CME", "BK", "PNC", "USB", "TFC", "CFG", "RF", "HBAN", "FITB", "KEY", "MTB", "NTRS", "STT", "AON", "AJG", "MMC", "TRV", "ALL", "PRU", "MET", "AFL", "CB", "CHRW", "WRB", "ACGL", "AIG", "HIG", "LNC", "PFG", "AMP", "TROW", "IVZ", "BEN", "APO", "BX", "KKR",
    
    # 醫療保健
    "JNJ", "UNH", "PFE", "ABBV", "LLY", "MRK", "TMO", "ABT", "DHR", "BMY", "AMGN", "GILD", "BIIB", "VRTX", "REGN", "ISRG", "BSX", "MDT", "SYK", "EW", "DXCM", "ALGN", "HCA", "CI", "ELV", "ANTM", "CVS", "MCK", "CAH", "ZTS", "ILMN", "MRNA", "GEHC", "BAX", "HOLX", "IDXX", "PKI", "WAT", "TECH", "SOLV", "INCY", "BMRN", "VTRS", "OGN", "MOH", "LH", "DGX", "A", "WST", "PODD", "STE", "CRL", "TDOC", "ZBH", "VAR", "RMD", "POOL", "HSIC", "TFX", "BDX", "DVA", "JCI", "MAS", "LKQ", "AME", "AMCR", "CTAS", "ITW", "PH", "ETN", "EMR", "DOV", "CMI", "DE", "CAT", "PCAR", "URI", "FAST", "NDSN", "SWK", "SNA", "HWM", "HON", "GE", "MMM", "BA", "LMT", "RTX", "NOC", "GD", "TDG", "LHX", "HII", "LDOS", "J", "FLT", "TDY", "R", "TXT", "L", "NVR", "LEN", "DHI", "PHM", "KBH", "MTH", "TMHC", "TPG", "RYL", "MHO", "LGIH", "CVCO", "MDC", "UDR", "AVB", "EQR", "ESS", "MAA", "CPT", "INVH", "AMT", "CCI", "SBAC", "PLD", "EXR", "PSA", "VICI", "O", "SPG", "REG", "KIM", "FRT", "BXP", "ARE", "VTR", "WELL", "PEAK", "DOC", "HR", "HST", "RLJ", "PK", "MAC", "SLG", "BXP", "ARE", "VTR", "WELL", "PEAK", "DOC", "HR", "HST", "RLJ", "PK", "MAC", "SLG",
    
    # 消費品
    "WMT", "PG", "KO", "PEP", "COST", "HD", "LOW", "TGT", "TJX", "SBUX", "MCD", "YUM", "CMG", "NKE", "LULU", "ULTA", "ROST", "DG", "DLTR", "BBY", "AZO", "ORLY", "GPC", "KMX", "CCL", "RCL", "NCLH", "MAR", "HLT", "WYNN", "LVS", "MGM", "CZR", "EXPE", "BKNG", "ABNB", "TRMB", "CHTR", "CMCSA", "DIS", "NFLX", "FOX", "FOXA", "PARA", "WBD", "LYV", "LEN", "NVR", "DHI", "PHM", "KBH", "MTH", "TMHC", "TPG", "RYL", "MHO", "LGIH", "CVCO", "MDC", "UDR", "AVB", "EQR", "ESS", "MAA", "CPT", "INVH", "AMT", "CCI", "SBAC", "PLD", "EXR", "PSA", "VICI", "O", "SPG", "REG", "KIM", "FRT", "BXP", "ARE", "VTR", "WELL", "PEAK", "DOC", "HR", "HST", "RLJ", "PK", "MAC", "SLG",
    
    # 能源
    "XOM", "CVX", "COP", "EOG", "SLB", "OXY", "KMI", "WMB", "OKE", "PSX", "MPC", "VLO", "HES", "FANG", "CTRA", "DVN", "PXD", "MRO", "APA", "NOV", "HAL", "BKR", "FTI", "LBRT", "WFRD", "CHX", "NEX", "AR", "MTDR", "SM", "RRC", "SWN", "NFG", "CNX", "BTU", "ARCH", "AM", "CEIX", "LNG", "KOS", "RIG", "DO", "SDRL", "VAL", "HP", "PTEN", "NBR", "WHD", "LBRT", "WFRD", "CHX", "NEX", "AR", "MTDR", "SM", "RRC", "SWN", "NFG", "CNX", "BTU", "ARCH", "AM", "CEIX", "LNG", "KOS", "RIG", "DO", "SDRL", "VAL", "HP", "PTEN", "NBR", "WHD",
    
    # 公用事業
    "NEE", "DUK", "SO", "AEP", "EXC", "XEL", "WEC", "ES", "PPL", "ED", "FE", "EIX", "SRE", "AWK", "AEE", "CNP", "CMS", "LNT", "DTE", "PEG", "NI", "AES", "CEG", "VST", "NRG", "EXE",
    
    # 材料
    "LIN", "APD", "SHW", "ECL", "DD", "DOW", "ALB", "FCX", "NEM", "PPG", "EMN", "LYB", "IP", "PKG", "SEE", "AVY", "WRK", "CC", "MOS", "CF", "NUE", "STLD", "CLF", "X", "AA", "CENX", "KALU", "CMC", "RS", "WOR", "ATI", "HAYN", "MTX", "TROX", "CBT", "HUN", "OLN", "WLK", "CE", "DCP", "KWR", "SLGN", "FUL", "SXT", "KOP", "IOSP", "HXL", "IFF", "AVNT", "ASH", "NGVT", "KRA", "KOP", "IOSP", "HXL", "IFF", "AVNT", "ASH", "NGVT", "KRA",
    
    # 工業
    "GE", "HON", "UPS", "FDX", "UNP", "NSC", "CSX", "KSU", "JBHT", "ODFL", "EXPD", "CHRW", "LSTR", "SAIA", "ARCB", "WERN", "HTLD", "KNX", "GXO", "RXO", "ZTO", "YMM", "AMZN", "WMT", "TGT", "COST", "HD", "LOW", "TJX", "ROST", "DG", "DLTR", "BBY", "AZO", "ORLY", "GPC", "KMX", "CCL", "RCL", "NCLH", "MAR", "HLT", "WYNN", "LVS", "MGM", "CZR", "EXPE", "BKNG", "ABNB", "TRMB", "CHTR", "CMCSA", "DIS", "NFLX", "FOX", "FOXA", "PARA", "WBD", "LYV",
    
    # 通訊服務
    "GOOGL", "GOOG", "META", "NFLX", "DIS", "CMCSA", "CHTR", "T", "VZ", "TMUS", "FOX", "FOXA", "PARA", "WBD", "LYV", "TRMB", "CHTR", "CMCSA", "DIS", "NFLX", "FOX", "FOXA", "PARA", "WBD", "LYV",
    
    # 房地產
    "AMT", "CCI", "SBAC", "PLD", "EXR", "PSA", "VICI", "O", "SPG", "REG", "KIM", "FRT", "BXP", "ARE", "VTR", "WELL", "PEAK", "DOC", "HR", "HST", "RLJ", "PK", "MAC", "SLG", "UDR", "AVB", "EQR", "ESS", "MAA", "CPT", "INVH", "AMT", "CCI", "SBAC", "PLD", "EXR", "PSA", "VICI", "O", "SPG", "REG", "KIM", "FRT", "BXP", "ARE", "VTR", "WELL", "PEAK", "DOC", "HR", "HST", "RLJ", "PK", "MAC", "SLG"
]

# 判斷上影線 + 第二天收復
# -----------------------------
def check_upper_shadow_reversal(df):
    if len(df) < 2:
        return False
    
    open1, close1, high1 = df['Open'].iloc[-2], df['Close'].iloc[-2], df['High'].iloc[-2]
    body1 = abs(close1 - open1)
    upper_shadow1 = high1 - max(open1, close1)
    
    close2 = df['Close'].iloc[-1]
    
    # 上影線長度需同時滿足：
    # 1) 長於實體的 shadow_ratio 倍
    # 2) 佔前一日收盤價至少 upper_shadow_min_pct
    upper_shadow_pct = (upper_shadow1 / close1) if close1 != 0 else 0
    first_day_shadow = (upper_shadow1 > body1 * shadow_ratio) and (upper_shadow_pct >= upper_shadow_min_pct)
    second_day_recover = close2 >= high1
    # 當天漲幅要 1% 以上
    daily_gain = (close2 - close1) / close1 >= 0.01
    
    return first_day_shadow and second_day_recover and daily_gain

# -----------------------------
# Slack 通知
# -----------------------------
def send_to_slack(webhook_url, text):
    try:
        resp = requests.post(webhook_url, json={"text": text}, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"Send Slack failed: {e}")

# -----------------------------
# 批次抓取（S&P 500 股票不多，可以一次抓）
# -----------------------------
data = yf.download(taiwan_stocks, period="5d", interval="1d", group_by='ticker', threads=True)
results = []
for ticker in taiwan_stocks:
    try:
        df = data[ticker] if len(taiwan_stocks) > 1 else data
        if check_upper_shadow_reversal(df):
            # 去除 .TW/.TWO 後再加入結果
            results.append(ticker.split('.')[0])
    except Exception as e:
        print(f"Error processing {ticker}: {e}")

# -----------------------------
# 顯示結果 + 發 Slack
# -----------------------------
print(results)

slack_webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
if slack_webhook:
    if results:
        tickers_str = ", ".join(results)
        send_to_slack(slack_webhook, f"台股篩選結果：{tickers_str}")
    else:
        send_to_slack(slack_webhook, "台股篩選結果：無符合條件標的")
else:
    print("No SLACK_WEBHOOK_URL set; skip Slack notification")