#include "docx_comment_parser.h"

#include <thread>
#include <mutex>
#include <atomic>
#include <queue>
#include <functional>
#include <condition_variable>
#include <unordered_map>

namespace docx {

// ─── Simple thread-pool ───────────────────────────────────────────────────────

class ThreadPool {
public:
    explicit ThreadPool(unsigned int n) {
        n = (n == 0) ? std::max(1u, std::thread::hardware_concurrency()) : n;
        workers_.reserve(n);
        for (unsigned i = 0; i < n; ++i)
            workers_.emplace_back([this]{ worker_loop(); });
    }

    ~ThreadPool() {
        {
            std::unique_lock<std::mutex> lk(mu_);
            stop_ = true;
        }
        cv_.notify_all();
        for (auto& t : workers_) t.join();
    }

    template<typename F>
    void submit(F&& f) {
        {
            std::unique_lock<std::mutex> lk(mu_);
            tasks_.push(std::forward<F>(f));
        }
        cv_.notify_one();
    }

    void wait_all() {
        std::unique_lock<std::mutex> lk(mu_);
        done_cv_.wait(lk, [this]{ return tasks_.empty() && active_ == 0; });
    }

private:
    void worker_loop() {
        while (true) {
            std::function<void()> task;
            {
                std::unique_lock<std::mutex> lk(mu_);
                cv_.wait(lk, [this]{ return stop_ || !tasks_.empty(); });
                if (stop_ && tasks_.empty()) return;
                task = std::move(tasks_.front());
                tasks_.pop();
                ++active_;
            }
            task();
            {
                std::unique_lock<std::mutex> lk(mu_);
                --active_;
            }
            done_cv_.notify_all();
        }
    }

    std::vector<std::thread>           workers_;
    std::queue<std::function<void()>>  tasks_;
    std::mutex                         mu_;
    std::condition_variable            cv_;
    std::condition_variable            done_cv_;
    bool                               stop_{false};
    int                                active_{0};
};

// ─── BatchParser::Impl ───────────────────────────────────────────────────────

struct BatchParser::Impl {
    unsigned int max_threads;

    struct FileResult {
        std::vector<CommentMetadata> comments;
        DocumentCommentStats         stats;
    };

    std::unordered_map<std::string, FileResult>   results_;
    std::unordered_map<std::string, std::string>  errors_;
    mutable std::mutex                             mu_;

    explicit Impl(unsigned int t) : max_threads(t) {}

    void parse_all(const std::vector<std::string>& paths) {
        ThreadPool pool(max_threads);

        for (const auto& p : paths) {
            pool.submit([this, p]{
                DocxParser parser;
                try {
                    parser.parse(p);
                    FileResult r;
                    r.comments = parser.comments();
                    r.stats    = parser.stats();
                    std::lock_guard<std::mutex> lk(mu_);
                    results_[p] = std::move(r);
                } catch (const std::exception& ex) {
                    std::lock_guard<std::mutex> lk(mu_);
                    errors_[p] = ex.what();
                }
            });
        }

        pool.wait_all();
    }
};

// ─── BatchParser public API ───────────────────────────────────────────────────

BatchParser::BatchParser(unsigned int max_threads)
    : impl_(std::make_unique<Impl>(max_threads)) {}

BatchParser::~BatchParser() = default;

void BatchParser::parse_all(const std::vector<std::string>& file_paths) {
    impl_->parse_all(file_paths);
}

const std::vector<CommentMetadata>& BatchParser::comments(const std::string& fp) const {
    auto it = impl_->results_.find(fp);
    if (it == impl_->results_.end())
        throw DocxParserError("File not parsed: " + fp);
    return it->second.comments;
}

const DocumentCommentStats& BatchParser::stats(const std::string& fp) const {
    auto it = impl_->results_.find(fp);
    if (it == impl_->results_.end())
        throw DocxParserError("File not parsed: " + fp);
    return it->second.stats;
}

const std::unordered_map<std::string, std::string>& BatchParser::errors() const noexcept {
    return impl_->errors_;
}

void BatchParser::release(const std::string& fp) {
    impl_->results_.erase(fp);
}

void BatchParser::release_all() {
    impl_->results_.clear();
}

} // namespace docx
