# utils/logger.py
import logging
import sys

def setup_logger(name: str = "LLM_Tracker") -> logging.Logger:
    """統一配置格式化日誌，便於 GitHub Actions 執行時查看詳細排錯日誌"""
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        formatter = logging.Formatter(
            '[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # 輸出到標準輸出
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        logger.addHandler(stdout_handler)
    return logger