# core/graph_matrix.py
from typing import List, Dict, Any

class GraphMatrixAnalyzer:
    @staticmethod
    def calculate_jaccard_similarity(tags1: List[str], tags2: List[str]) -> float:
        """計算兩個關鍵字集合的傑卡德相似度 (IoU)"""
        set1 = set([t.lower().strip() for t in tags1])
        set2 = set([t.lower().strip() for t in tags2])
        if not set1 or not set2:
            return 0.0
        return float(len(set1.intersection(set2))) / len(set1.union(set2))

    def generate_relations(self, all_videos_pool: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        【全量標籤矩陣核心算法】
        不依賴向量數據庫，跨越 3 個月前的爆款老片，全量動態增量關聯
        """
        themes_matrix: Dict[str, List[Dict[str, str]]] = {}
        
        # 1. 構建全域主題映射 (橫向對比矩陣)
        for vid in all_videos_pool:
            topics = vid.get("ai_topics", [])
            for topic in topics:
                topic_clean = topic.strip()
                if not topic_clean:
                    continue
                if topic_clean not in themes_matrix:
                    themes_matrix[topic_clean] = []
                
                if not any(item["video_id"] == vid["video_id"] for item in themes_matrix[topic_clean]):
                    themes_matrix[topic_clean].append({
                        "video_id": vid["video_id"],
                        "channel": vid["channel"],
                        "title": vid["title"]
                    })

        # 2. 全量歷史影片 Jaccard 相似度重新計算，刷新關聯鏈
        for i, target_vid in enumerate(all_videos_pool):
            related_list = []
            target_topics = target_vid.get("ai_topics", [])
            
            for j, candidate_vid in enumerate(all_videos_pool):
                if i == j: 
                    continue
                
                candidate_topics = candidate_vid.get("ai_topics", [])
                score = self.calculate_jaccard_similarity(target_topics, candidate_topics)
                
                if score > 0.1: # 設低門檻，確保關聯有效性
                    related_list.append({
                        "video_id": candidate_vid["video_id"],
                        "title": candidate_vid["title"],
                        "score": score
                    })
            
            related_list.sort(key=lambda x: x["score"], reverse=True)
            
            # 更新最新 Top 3 關聯，完美消除「冷啟動問題」
            target_vid["related_videos"] = [
                {
                    "video_id": item["video_id"],
                    "title": item["title"],
                    "reason": f"Shared profile overlap ({int(item['score']*100)}% tag similarity)."
                }
                for item in related_list[:3]
            ]

        return themes_matrix