SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ----------------------------
-- Table structure for chat_list
-- ----------------------------
DROP TABLE IF EXISTS `chat_list`;
CREATE TABLE `chat_list`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '记录ID',
  `user_id` int NOT NULL COMMENT '关联用户ID',
  `session_id` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '会话ID',
  `title` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '会话标题',
  `create_time` datetime NULL DEFAULT NULL COMMENT '会话创建时间',
  `update_time` datetime NULL DEFAULT CURRENT_TIMESTAMP COMMENT '最后消息更新时间',
  PRIMARY KEY (`id`) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 1 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_general_ci COMMENT = '用户聊天历史记录' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for conversation_detail
-- ----------------------------
DROP TABLE IF EXISTS `conversation_detail`;
CREATE TABLE `conversation_detail`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  `session_id` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '会话ID，关联chat_history表',
  `from_who` varchar(4) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '消息来源: user-用户, ai-AI',
  `round` int NULL DEFAULT 1 COMMENT '会话轮次顺序',
  `timestamp` datetime NOT NULL COMMENT '消息时间戳(毫秒)',
  `uuid` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NULL DEFAULT NULL COMMENT '问答对唯一标识',
  `content` mediumtext CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '用户/AI的对话内容',
  `think_msg` mediumtext CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NULL COMMENT 'AI思考过程内容',
  `create_time` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录创建时间',
  `has_report` tinyint(1) NULL DEFAULT NULL COMMENT '是否有报告',
  `report_title` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NULL DEFAULT '研究报告' COMMENT '报告标题（仅AI消息且has_report=1时有效）',
  PRIMARY KEY (`id`) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 1 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_general_ci COMMENT = '会话详情表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for user_files
-- ----------------------------
DROP TABLE IF EXISTS `user_files`;
CREATE TABLE `user_files`  (
  `id` int NOT NULL AUTO_INCREMENT COMMENT '自增ID',
  `file_id` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '文件唯一标识UUID',
  `user_id` int NOT NULL COMMENT '用户ID',
  `original_filename` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '原始文件名',
  `stored_filename` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '存储文件名（UUID+扩展名）',
  `file_path` varchar(512) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '文件完整路径',
  `file_size` bigint NOT NULL COMMENT '文件大小（字节）',
  `file_type` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NOT NULL COMMENT '文件类型（扩展名）',
  `status` enum('processing','processed','failed') CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci NULL DEFAULT 'processing' COMMENT '文件处理状态',
  `upload_time` datetime NOT NULL COMMENT '上传时间',
  `process_time` datetime NULL DEFAULT NULL COMMENT '处理完成时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `file_id`(`file_id` ASC) USING BTREE,
  INDEX `idx_user_id`(`user_id` ASC) USING BTREE,
  INDEX `idx_file_id`(`file_id` ASC) USING BTREE,
  INDEX `idx_upload_time`(`upload_time` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 1 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_general_ci COMMENT = '用户文件表' ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for users
-- ----------------------------
DROP TABLE IF EXISTS `users`;
CREATE TABLE `users`  (
  `id` int NOT NULL AUTO_INCREMENT,
  `username` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `email` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `password` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `avatar` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL,
  `createtime` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updatetime` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `username`(`username` ASC) USING BTREE,
  UNIQUE INDEX `email`(`email` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 1 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci ROW_FORMAT = DYNAMIC;

SET FOREIGN_KEY_CHECKS = 1;
